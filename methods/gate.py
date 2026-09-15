#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Admission gate — read the counter, refuse once a limit is reached

Micro-USD integers throughout, so no float math ever decides whether a call is refused.
Deliberately not reservation-based: calls concurrent with an admission can overshoot a limit
by one call's cost each, which two months of production on the LiteLLM path measured at well
under 1% and is not worth the machinery to close.
"""

import datetime

import cachetools  # pylint: disable=E0401

from sqlalchemy import select  # pylint: disable=E0401

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import context, db  # pylint: disable=E0401

from ._counters import PERIOD_MONTH, member_key, period_start, project_key
from ..models.usage_counter import UsageCounter

SCOPE_PROJECT = "project"
SCOPE_MEMBER = "member"

KEY_PREFIX = "usage"
QUEUE_KEY = f"{KEY_PREFIX}:queue:events"
DRAIN_LEASE_KEY = f"{KEY_PREFIX}:drain:lease"

DEFAULT_LIMITS_CACHE_TTL = 30

# One resolve per (project, user) per window; the ladder is an RPC, the gate is per call
_limits_cache = None
_primed = cachetools.TTLCache(maxsize=16384, ttl=3600)

# KEYS: 1=counter hash · ARGV: 1=persisted counter
# Raise-to-max, not HSETNX: a warm key left behind while budgets were disabled sits below
# real spend, and skipping it would resume enforcement from a stale figure.
PRIME_LUA = """
local persisted = tonumber(ARGV[1])
local current = redis.call('HGET', KEYS[1], 'counter')
if current == false or tonumber(current) < persisted then
    redis.call('HSET', KEYS[1], 'counter', persisted)
    return 1
end
return 0
"""


def period_of(moment):
    """'YYYYMM' of the period a moment falls in."""
    return f"{period_start(moment, PERIOD_MONTH):%Y%m}"


def project_hash_key(project_id, moment):
    """Redis hash holding a project's running spend for the period."""
    return f"{KEY_PREFIX}:ctr:p:{int(project_id)}:{period_of(moment)}"


def member_hash_key(project_id, user_id, moment):
    """Same, for one member's slice."""
    return f"{KEY_PREFIX}:ctr:u:{int(project_id)}:{int(user_id)}:{period_of(moment)}"


def limits_cache(ttl_seconds):
    """Built on first use so the configured TTL applies instead of a value fixed at import."""
    global _limits_cache  # pylint: disable=W0603
    #
    if _limits_cache is None:
        _limits_cache = cachetools.TTLCache(maxsize=8192, ttl=max(1, int(ttl_seconds)))
    #
    return _limits_cache


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_gate_limits(self, project_id, user_id=None):
        """Effective limits in micro-USD from the one canonical ladder, cached briefly.

        None anywhere means unlimited; the caller cannot tell an unlimited limit from a
        failed read, which is why a failure raises instead of answering.
        """
        ttl = (self.usage_config().get("limits") or {}).get(
            "cache_ttl_seconds", DEFAULT_LIMITS_CACHE_TTL,
        )
        cache = limits_cache(ttl)
        #
        cache_key = (int(project_id), None if user_id is None else int(user_id))
        cached = cache.get(cache_key)
        #
        if cached is not None:
            return cached
        #
        limits = context.rpc_manager.timeout(10).elitea_core_get_effective_budget_limits(
            project_id=project_id, user_id=user_id,
        ) or {}
        #
        cache[cache_key] = limits
        #
        return limits

    @web.method()
    def usage_gate_check(self, project_id, user_id=None, moment=None):
        """Is a budget already full? {"closed", "scope", "healthy"}.

        Read-only: the whole admission decision, and also the advisory pre-check other
        planes call. healthy=False means the answer is unknown, never that it is "no".
        """
        moment = moment or datetime.datetime.now(datetime.timezone.utc)
        unknown = {"closed": False, "scope": None, "healthy": False}
        #
        try:
            limits = self.usage_gate_limits(project_id, user_id)
        except:  # pylint: disable=W0702
            log.exception("usage: cannot resolve limits for project %s", project_id)
            return unknown
        #
        if not limits.get("enabled", False):
            return {"closed": False, "scope": None, "healthy": True}
        #
        scopes = [(
            SCOPE_PROJECT, project_hash_key(project_id, moment),
            project_key(project_id, moment), limits.get("project_limit_micro"),
        )]
        #
        if user_id:
            scopes.append((
                SCOPE_MEMBER, member_hash_key(project_id, user_id, moment),
                member_key(project_id, user_id, moment), limits.get("member_limit_micro"),
            ))
        #
        try:
            client = self.usage_redis_client()
            #
            for scope, hash_key, counter_key, limit in scopes:
                if limit is None:
                    continue
                #
                self.usage_gate_prime(hash_key, counter_key)
                #
                if int(client.hget(hash_key, "counter") or 0) >= max(0, int(limit)):
                    return {"closed": True, "scope": scope, "healthy": True}
        except:  # pylint: disable=W0702
            log.exception("usage: cannot read counters for project %s", project_id)
            return unknown
        #
        return {"closed": False, "scope": None, "healthy": True}

    @web.method()
    def usage_gate_accrue(self, project_id, user_id, actual_micro, moment=None):
        """Add what a finished call cost to the counters the gate reads. True when accrued."""
        actual = max(0, int(actual_micro or 0))
        #
        if not actual or project_id is None:
            return False
        #
        moment = moment or datetime.datetime.now(datetime.timezone.utc)
        #
        try:
            client = self.usage_redis_client()
            client.hincrby(project_hash_key(project_id, moment), "counter", actual)
            #
            if user_id:
                client.hincrby(member_hash_key(project_id, user_id, moment), "counter", actual)
            #
            return True
        except:  # pylint: disable=W0702
            # Postgres still has the row, so the hourly reconcile repairs the drift
            log.exception("usage: failed to accrue spend for project %s", project_id)
            return False

    @web.method()
    def usage_gate_prime(self, hash_key, counter_key):
        """Seed a cold or stale hash from Postgres, never lowering a counter that is ahead.

        Without this an eviction silently resets the counter to zero and lets spend run away.
        """
        if hash_key in _primed:
            return
        #
        _primed[hash_key] = True
        #
        try:
            self.usage_redis_client().eval(
                PRIME_LUA, 1, hash_key, self.usage_counter_of(counter_key),
            )
        except:  # pylint: disable=W0702
            # Retry on the next call rather than leaving an unprimed key marked primed
            _primed.pop(hash_key, None)
            raise

    @web.method()
    def usage_counter_of(self, counter_key):
        """Persisted cost for one counter row, in micro-USD. 0 when there is no row yet."""
        statement = select(UsageCounter.cost_micro_usd).where(*(
            getattr(UsageCounter, column) == value for column, value in counter_key.items()
        ))
        #
        with db.engine.connect() as connection:
            return int(connection.execute(statement).scalar() or 0)

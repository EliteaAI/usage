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

""" Admission gate — reservation-based, one Redis round trip per decision

available = limit - (counter + reserved). Micro-USD integers throughout, so no float
math ever decides whether a call is refused.
"""

import datetime
import json
import time
import uuid

import cachetools  # pylint: disable=E0401

from sqlalchemy import select  # pylint: disable=E0401

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import context, db  # pylint: disable=E0401

from ._counters import PERIOD_MONTH, member_key, period_start, project_key
from ..models.usage_counter import UsageCounter

SCOPE_PROJECT = "project"
SCOPE_MEMBER = "member"

UNLIMITED = -1

KEY_PREFIX = "usage"
RESV_INDEX_KEY = f"{KEY_PREFIX}:resv:index"
QUEUE_KEY = f"{KEY_PREFIX}:queue:events"
DRAIN_LEASE_KEY = f"{KEY_PREFIX}:drain:lease"
REAP_LEASE_KEY = f"{KEY_PREFIX}:reap:lease"

DEFAULT_LIMITS_CACHE_TTL = 30

# One resolve per (project, user) per window; the ladder is an RPC, the gate is per call
_limits_cache = None
_primed = cachetools.TTLCache(maxsize=16384, ttl=3600)

# KEYS: 1=project hash, 2=member hash or '', 3=resv zset, 4=resv index
# ARGV: 1=estimate, 2=project limit (-1 unlimited), 3=member limit, 4=deadline ms,
#       5=index member, 6=reservation member json
GATE_LUA = """
local est = tonumber(ARGV[1])
local pc = tonumber(redis.call('HGET', KEYS[1], 'counter') or '0')
local pr = tonumber(redis.call('HGET', KEYS[1], 'reserved') or '0')
local plim = tonumber(ARGV[2])
if plim >= 0 and (pc + pr + est) > plim then return {0, 'project'} end
local mlim = tonumber(ARGV[3])
if KEYS[2] ~= '' and mlim >= 0 then
    local mc = tonumber(redis.call('HGET', KEYS[2], 'counter') or '0')
    local mr = tonumber(redis.call('HGET', KEYS[2], 'reserved') or '0')
    if (mc + mr + est) > mlim then return {0, 'member'} end
end
redis.call('HINCRBY', KEYS[1], 'reserved', est)
if KEYS[2] ~= '' then redis.call('HINCRBY', KEYS[2], 'reserved', est) end
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[6])
redis.call('SADD', KEYS[4], ARGV[5])
return {1, ARGV[6]}
"""

# KEYS: 1=resv zset, 2=project hash, 3=member hash or ''
# ARGV: 1=reservation member json, 2=estimate, 3=actual
# Release is once-only (ZREM guards it). Accrual is not: a reservation reaped early still
# has to bill what the call actually cost.
SETTLE_LUA = """
local removed = redis.call('ZREM', KEYS[1], ARGV[1])
if removed == 1 then
    redis.call('HINCRBY', KEYS[2], 'reserved', -tonumber(ARGV[2]))
    if KEYS[3] ~= '' then redis.call('HINCRBY', KEYS[3], 'reserved', -tonumber(ARGV[2])) end
end
local actual = tonumber(ARGV[3])
if actual > 0 then
    redis.call('HINCRBY', KEYS[2], 'counter', actual)
    if KEYS[3] ~= '' then redis.call('HINCRBY', KEYS[3], 'counter', actual) end
end
return removed
"""


def period_of(moment):
    """'YYYYMM' of the period a moment falls in."""
    return f"{period_start(moment, PERIOD_MONTH):%Y%m}"


def project_hash_key(project_id, moment):
    """Redis hash holding a project's counter and outstanding reservations."""
    return f"{KEY_PREFIX}:ctr:p:{int(project_id)}:{period_of(moment)}"


def member_hash_key(project_id, user_id, moment):
    """Same, for one member's slice."""
    return f"{KEY_PREFIX}:ctr:u:{int(project_id)}:{int(user_id)}:{period_of(moment)}"


def resv_key(project_id, moment):
    """Reservation ZSET, scored by deadline so the reaper can range-scan it."""
    return f"{KEY_PREFIX}:resv:{int(project_id)}:{period_of(moment)}"


def resv_index_member(project_id, moment):
    """What the reaper iterates: one entry per live (project, period)."""
    return f"{int(project_id)}:{period_of(moment)}"


def resv_key_of_index_member(index_member):
    """Back from an index entry to its ZSET key — what the reaper iterates with."""
    return f"{KEY_PREFIX}:resv:{index_member}"


def limits_cache(ttl_seconds):
    """Built on first use so the configured TTL applies instead of a value fixed at import."""
    global _limits_cache  # pylint: disable=W0603
    #
    if _limits_cache is None:
        _limits_cache = cachetools.TTLCache(maxsize=8192, ttl=max(1, int(ttl_seconds)))
    #
    return _limits_cache


def as_limit(value):
    """Micro-USD limit as the Lua expects it: -1 for unlimited, otherwise a non-negative int."""
    if value is None:
        return UNLIMITED
    #
    return max(0, int(value))


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
    def usage_gate_acquire(self, project_id, user_id, estimate_micro, moment):  # pylint: disable=R0914
        """Reserve an estimate, or refuse. {"allowed", "scope", "reservation", "healthy"}.

        healthy=False means the decision could not be made at all — the caller decides what
        that costs, fail-closed in enforce and fail-open otherwise.
        """
        refused = {"allowed": False, "scope": None, "reservation": None, "healthy": False}
        #
        try:
            limits = self.usage_gate_limits(project_id, user_id)
        except:  # pylint: disable=W0702
            log.exception("usage: cannot resolve limits for project %s", project_id)
            return refused
        #
        if not limits.get("enabled", False):
            return {"allowed": True, "scope": None, "reservation": None, "healthy": True}
        #
        project_key_name = project_hash_key(project_id, moment)
        member_key_name = "" if not user_id else member_hash_key(project_id, user_id, moment)
        estimate = max(0, int(estimate_micro or 0))
        #
        reservation = json.dumps({
            "id": self.usage_reservation_id(),
            "est": estimate,
            "pk": project_key_name,
            "mk": member_key_name,
            "rk": resv_key(project_id, moment),
        }, sort_keys=True)
        #
        try:
            self.usage_gate_prime(project_key_name, project_key(project_id, moment))
            #
            if member_key_name:
                self.usage_gate_prime(
                    member_key_name, member_key(project_id, user_id, moment),
                )
            #
            allowed, detail = self.usage_redis_client().eval(
                GATE_LUA, 4,
                project_key_name, member_key_name,
                resv_key(project_id, moment), RESV_INDEX_KEY,
                estimate,
                as_limit(limits.get("project_limit_micro")),
                as_limit(limits.get("member_limit_micro")),
                self.usage_reservation_deadline_ms(),
                resv_index_member(project_id, moment),
                reservation,
            )
        except:  # pylint: disable=W0702
            log.exception("usage: admission gate is unreachable for project %s", project_id)
            return refused
        #
        if not int(allowed):
            return {
                "allowed": False, "scope": str(detail), "reservation": None, "healthy": True,
            }
        #
        return {"allowed": True, "scope": None, "reservation": reservation, "healthy": True}

    @web.method()
    def usage_gate_check(self, project_id, user_id=None, moment=None):
        """Is a budget already full? Read-only — reserves nothing, so it can never deny wrongly.

        {"closed", "scope", "healthy"}. healthy=False means the answer is unknown.
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
            limits.get("project_limit_micro"),
        )]
        #
        if user_id:
            scopes.append((
                SCOPE_MEMBER, member_hash_key(project_id, user_id, moment),
                limits.get("member_limit_micro"),
            ))
        #
        try:
            client = self.usage_redis_client()
            #
            for scope, hash_key, limit in scopes:
                if limit is None:
                    continue
                #
                spent = sum(
                    int(value or 0)
                    for value in client.hmget(hash_key, "counter", "reserved")
                )
                #
                if spent >= max(0, int(limit)):
                    return {"closed": True, "scope": scope, "healthy": True}
        except:  # pylint: disable=W0702
            log.exception("usage: cannot read counters for project %s", project_id)
            return unknown
        #
        return {"closed": False, "scope": None, "healthy": True}

    @web.method()
    def usage_gate_settle(self, reservation, actual_micro):
        """Release a reservation and accrue what the call really cost. True when released."""
        try:
            parsed = json.loads(reservation)
        except (TypeError, ValueError):
            log.error("usage: cannot settle a malformed reservation")
            return False
        #
        try:
            removed = self.usage_redis_client().eval(
                SETTLE_LUA, 3,
                parsed["rk"], parsed["pk"], parsed.get("mk") or "",
                reservation, int(parsed.get("est") or 0), max(0, int(actual_micro or 0)),
            )
            #
            return bool(int(removed))
        except:  # pylint: disable=W0702
            log.exception("usage: failed to settle a reservation")
            return False

    @web.method()
    def usage_gate_release(self, reservation):
        """Drop a reservation without billing anything — the reaper's call."""
        return self.usage_gate_settle(reservation, 0)

    @web.method()
    def usage_gate_prime(self, hash_key, counter_key):
        """Seed a cold hash from Postgres. HSETNX, so racers are harmless and a warm key is kept.

        Without this an eviction silently resets the counter to zero and lets spend run away.
        """
        if hash_key in _primed:
            return
        #
        _primed[hash_key] = True
        #
        try:
            self.usage_redis_client().hsetnx(
                hash_key, "counter", self.usage_counter_of(counter_key),
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

    @web.method()
    def usage_reservation_id(self):
        """Unique per acquire, so two concurrent calls never share one ZSET member."""
        return uuid.uuid4().hex

    @web.method()
    def usage_reservation_deadline_ms(self):
        """When the reaper may reclaim this reservation. Must outlive the proxy's own timeout."""
        ttl = int((self.usage_config().get("reservation") or {}).get("ttl_seconds", 900))
        #
        return int(time.time() * 1000) + max(1, ttl) * 1000

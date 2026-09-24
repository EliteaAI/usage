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

""" Budget warning thresholds

Lives beside the gate on purpose: the limits and the counters a warning needs are the same
two numbers the gate already resolved, so warning for any inference plane comes for free.
"""

import datetime

import cachetools  # pylint: disable=E0401

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from .gate import SCOPE_MEMBER, SCOPE_PROJECT, member_hash_key, project_hash_key
from .mode import MODE_ENFORCE

DEFAULT_WARNING_PCT = 80

# Config key per warning scope. "member" is the gate's name for what the UI calls "user".
WARNING_PCT_KEYS = {
    "project": "project_pct",
    "personal_project": "personal_project_pct",
    "user": "user_pct",
}

# A page load asks on every chat, agent and pipeline open; one read per scope per minute
# is shared by every member of the project.
BUDGET_WARNING_TTL = 60.0

NO_WARNING = {
    "scope": None, "percent_used": None, "warning_pct": None, "should_warn": False,
}

_warning_cache = cachetools.TTLCache(maxsize=8192, ttl=BUDGET_WARNING_TTL)


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_get_warning_threshold(self, scope):
        """Percent-of-limit at which the UI warns for a budget scope.

        Falls back to the default for an unknown scope or an out-of-range value, so a bad
        config degrades to the previous behaviour rather than silencing warnings.
        """
        thresholds = self.usage_config().get("warning_thresholds", None) or {}
        #
        try:
            value = int(thresholds.get(WARNING_PCT_KEYS.get(scope, ""), DEFAULT_WARNING_PCT))
        except (TypeError, ValueError):
            return DEFAULT_WARNING_PCT
        #
        return value if 1 <= value <= 100 else DEFAULT_WARNING_PCT

    @web.method()
    def usage_get_budget_warning_state(self, project_id, user_id=None):
        """Whether to warn this user that a budget is nearing its limit, and which one.

        The member budget wins over the project budget and only one scope is ever returned,
        so the UI has no precedence rule to get wrong.
        """
        key = (int(project_id), None if user_id is None else int(user_id))
        #
        cached = _warning_cache.get(key)
        #
        if cached is not None:
            return cached
        #
        payload = self.usage_resolve_budget_warning(project_id, user_id)
        _warning_cache[key] = payload
        #
        return payload

    @web.method()
    def usage_resolve_budget_warning(self, project_id, user_id=None):
        """Compute the warning state, ignoring the cache. See usage_get_budget_warning_state."""
        # Observe mode tracks spend but never blocks, so warning that requests are about to
        # become unavailable would not be true
        if self.usage_get_mode() != MODE_ENFORCE:
            return NO_WARNING
        #
        moment = datetime.datetime.now(datetime.timezone.utc)
        #
        try:
            limits = self.usage_gate_limits(project_id, user_id)
            #
            if not limits.get("enabled", False):
                return NO_WARNING
            #
            personal = bool(limits.get("is_personal_project", False))
            #
            # Member first: it is the one that stops this user specifically
            scopes = []
            #
            if user_id:
                scopes.append((
                    SCOPE_MEMBER, "user",
                    member_hash_key(project_id, user_id, moment),
                    limits.get("member_limit_nano"),
                ))
            #
            scopes.append((
                SCOPE_PROJECT, "personal_project" if personal else "project",
                project_hash_key(project_id, moment),
                limits.get("project_limit_nano"),
            ))
            #
            client = self.usage_redis_client()
            #
            for scope, threshold_scope, hash_key, limit in scopes:
                if limit is None or int(limit) <= 0:
                    continue
                #
                # Spend only, like the read-only door: a warning banner must track money spent,
                # not reservations that may never be billed
                spent = int(client.hget(hash_key, "counter") or 0)
                #
                state = self.usage_warning_for_scope(
                    scope, spent, int(limit), self.usage_get_warning_threshold(threshold_scope),
                )
                #
                if state is not None:
                    return state
        except:  # pylint: disable=W0702
            log.exception(
                "usage: failed to resolve budget warning for project %s user %s",
                project_id, user_id,
            )
        #
        return NO_WARNING

    @web.method()
    def usage_warning_for_scope(self, scope, used_nano, limit_nano, threshold_pct):
        """Warning state for one scope, or None when that scope has nothing to warn about."""
        if limit_nano <= 0:
            return None
        #
        pct = used_nano / limit_nano * 100
        #
        # At or over the limit the refusal itself is the message, so this banner stays out of
        # the way -- and a stale reading cannot contradict a rejection the user just saw
        if pct < threshold_pct or pct >= 100:
            return None
        #
        return {
            "scope": scope,
            "percent_used": round(pct),
            "warning_pct": threshold_pct,
            "should_warn": True,
        }

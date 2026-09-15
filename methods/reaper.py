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

""" Reservation reaper — the sole backstop for a call that never reached settle """

import time

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from .gate import PRUNE_LUA, RESV_INDEX_KEY, resv_key_of_index_member

REAP_LIMIT = 500


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_reap_expired(self):
        """Release every reservation past its deadline. Returns how many were released."""
        try:
            entries = self.usage_redis_client().smembers(RESV_INDEX_KEY) or []
        except:  # pylint: disable=W0702
            log.exception("usage: failed to read the reservation index")
            return 0
        #
        released = 0
        #
        for entry in entries:
            released += self.usage_reap_bucket(entry)
        #
        return released

    @web.method()
    def usage_reap_bucket(self, index_member):
        """ Method """
        now_ms = int(time.time() * 1000)
        released = 0
        resv_key = resv_key_of_index_member(index_member)
        #
        try:
            client = self.usage_redis_client()
            expired = client.zrangebyscore(
                resv_key, 0, now_ms, start=0, num=REAP_LIMIT,
            ) or []
        except:  # pylint: disable=W0702
            log.exception("usage: failed to scan reservations for %s", index_member)
            return 0
        #
        for reservation in expired:
            # ZREM inside settle decides the single winner, so a racing reaper releases nothing
            if self.usage_gate_release(reservation):
                released += 1
        #
        if released:
            log.warning("usage: released %s expired reservation(s) for %s", released, index_member)
        #
        self.usage_forget_drained_bucket(index_member, resv_key)
        #
        return released

    @web.method()
    def usage_forget_drained_bucket(self, index_member, resv_key):
        """Drop an emptied bucket from the index, or the reaper scans every month ever gated."""
        try:
            # One script: a check-then-SREM would drop a bucket a concurrent acquire just
            # reserved into, hiding that reservation from the reaper for the rest of the month
            self.usage_redis_client().eval(
                PRUNE_LUA, 2, resv_key, RESV_INDEX_KEY, index_member,
            )
        except:  # pylint: disable=W0702
            log.exception("usage: failed to prune the reservation index for %s", index_member)

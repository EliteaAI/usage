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

""" Background workers — one leaseholder drains, one reaps; the other replicas idle """

import threading
import time
import uuid

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from .gate import DRAIN_LEASE_KEY, REAP_LEASE_KEY

DEFAULT_FLUSH_INTERVAL = 5
DEFAULT_REAP_INTERVAL = 30
DEFAULT_LEASE_SECONDS = 20

REPLICA_ID = uuid.uuid4().hex


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_start_workers(self):
        """Daemon threads, not gevent: these do blocking DB work and must not sit on the hub."""
        if getattr(self, "_usage_workers_started", False):
            return
        #
        self._usage_workers_started = True
        #
        for name, tick, interval_key, default in (
                ("usage-drainer", self.usage_drain_batch,
                 "queue_flush_interval_seconds", DEFAULT_FLUSH_INTERVAL),
                ("usage-reaper", self.usage_reap_expired,
                 "reaper_interval_seconds", DEFAULT_REAP_INTERVAL),
        ):
            lease_key = DRAIN_LEASE_KEY if name == "usage-drainer" else REAP_LEASE_KEY
            #
            threading.Thread(
                target=self.usage_worker_loop,
                args=(name, tick, lease_key, interval_key, default),
                name=name, daemon=True,
            ).start()

    @web.method()
    def usage_worker_loop(self, name, tick, lease_key, interval_key, default_interval):  # pylint: disable=R0913,R0917
        """ Method """
        log.info("usage: %s started", name)
        #
        while True:
            redis_config = self.usage_config().get("redis") or {}
            interval = max(1, int(redis_config.get(interval_key, default_interval)))
            #
            try:
                if self.usage_hold_lease(lease_key):
                    tick()
            except:  # pylint: disable=W0702
                log.exception("usage: %s tick failed", name)
            #
            time.sleep(interval)

    @web.method()
    def usage_hold_lease(self, lease_key):
        """True while this replica owns the lease. Losers idle rather than double-drain."""
        redis_config = self.usage_config().get("redis") or {}
        lease_seconds = max(2, int(redis_config.get("lease_seconds", DEFAULT_LEASE_SECONDS)))
        #
        try:
            client = self.usage_redis_client()
            #
            if client.set(lease_key, REPLICA_ID, nx=True, ex=lease_seconds):
                return True
            #
            # Refresh only our own lease: a plain EXPIRE would let a loser keep the winner alive
            if client.get(lease_key) == REPLICA_ID:
                client.expire(lease_key, lease_seconds)
                return True
        except:  # pylint: disable=W0702
            log.exception("usage: failed to take the %s lease", lease_key)
        #
        return False

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

""" usage_event writer — the direct-insert fallback for when the queue is unavailable """

from sqlalchemy.dialects.postgresql import insert

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db  # pylint: disable=E0401

from ._counters import EVENT_TYPE_LLM
from .drainer import counter_deltas
from ..models.usage_event import UsageEvent


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_write_event(self, row):
        """True when a row landed. At-least-once delivery is safe: a repeat of the same
        (idempotency_key, ts) is dropped by the partitioned unique index."""
        payload = dict(row)
        #
        statement = insert(UsageEvent).values(**payload).on_conflict_do_nothing(
            index_elements=["idempotency_key", "ts"],
        ).returning(UsageEvent.ts, UsageEvent.project_id, UsageEvent.user_id,
                    UsageEvent.input_tokens, UsageEvent.output_tokens, UsageEvent.cost_micro_usd)
        #
        try:
            with db.engine.connect() as connection:
                landed = [dict(row) for row in connection.execute(statement).mappings()]
                # Only LLM rows feed the counters, and off the queue the drainer's RETURNING
                # never sees them, so this is their only count
                if payload.get("event_type") == EVENT_TYPE_LLM:
                    self.usage_apply_counter_deltas(connection, counter_deltas(landed))
                connection.commit()
            #
            return bool(landed)
        except:  # pylint: disable=W0702
            # Losing a row must never break the response the user is already reading
            log.exception("usage: failed to write usage_event")
            return False

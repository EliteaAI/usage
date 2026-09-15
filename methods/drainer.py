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

""" Write-behind drainer — queue to usage_event, usage_event to usage_counter """

import json

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db  # pylint: disable=E0401

from ._counters import member_key, project_key
from .gate import QUEUE_KEY
from ..models.usage_counter import UsageCounter
from ..models.usage_event import UsageEvent

DEFAULT_BATCH_SIZE = 500


def period_of(ts):
    """Denormalised 'YYYYMM' of a row timestamp; accepts the queue's ISO string too."""
    return f"{ts:%Y%m}" if hasattr(ts, "strftime") else str(ts)[:4] + str(ts)[5:7]


COUNTER_INDEX = ["project_id", "user_id", "period_kind", "period_start", "model_name"]

# What the counters accumulate; the RETURNING projection is the same list plus the row's identity
MEASURES = ("input_tokens", "output_tokens", "cost_micro_usd")

RETURNING_COLUMNS = (
    UsageEvent.ts, UsageEvent.project_id, UsageEvent.user_id,
    UsageEvent.input_tokens, UsageEvent.output_tokens, UsageEvent.cost_micro_usd,
)


def counter_deltas(rows):
    """Per-counter-key sums for rows that actually landed. Two keys per row: project and member."""
    deltas = {}
    #
    for row in rows:
        keys = [project_key(row["project_id"], row["ts"])]
        #
        if row.get("user_id"):
            keys.append(member_key(row["project_id"], row["user_id"], row["ts"]))
        #
        for key in keys:
            bucket = deltas.setdefault(
                tuple(sorted(key.items())),
                dict(key, call_count=0, **{measure: 0 for measure in MEASURES}),
            )
            bucket["call_count"] += 1
            #
            for measure in MEASURES:
                bucket[measure] += int(row.get(measure) or 0)
    #
    return list(deltas.values())


def counter_upsert(delta):
    """Accumulating upsert: concurrent drainers and a repeated tick must add, never overwrite."""
    statement = insert(UsageCounter).values(**delta)
    #
    return statement.on_conflict_do_update(
        index_elements=COUNTER_INDEX,
        set_={
            "input_tokens": UsageCounter.input_tokens + statement.excluded.input_tokens,
            "output_tokens": UsageCounter.output_tokens + statement.excluded.output_tokens,
            "cost_micro_usd": UsageCounter.cost_micro_usd + statement.excluded.cost_micro_usd,
            "call_count": UsageCounter.call_count + statement.excluded.call_count,
            "updated_at": func.now(),
        },
    )


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_enqueue_event(self, row):
        """True when the row is queued. False sends the caller to the direct-insert fallback."""
        try:
            self.usage_redis_client().rpush(QUEUE_KEY, json.dumps(row, default=str))
            return True
        except:  # pylint: disable=W0702
            log.exception("usage: failed to enqueue a usage_event row")
            return False

    @web.method()
    def usage_drain_batch(self):
        """One tick: queued facts to usage_event, then landed facts to usage_counter."""
        rows = self.usage_dequeue_events()
        drained = 0
        #
        try:
            with db.engine.connect() as connection:
                if rows:
                    drained = len(self.usage_insert_events(connection, rows))
                #
                connection.commit()
        except:  # pylint: disable=W0702
            log.exception("usage: drain tick failed")
            # Safe to retry because the insert is idempotent on (idempotency_key, ts); dropping
            # the batch instead would lose the facts for good, reconcile included
            self.usage_requeue_events(rows)
            #
            return 0
        #
        return drained

    @web.method()
    def usage_requeue_events(self, rows):
        """Put a failed batch back at the head of the queue, oldest first."""
        if not rows:
            return
        #
        try:
            client = self.usage_redis_client()
            #
            for row in reversed(rows):
                client.lpush(QUEUE_KEY, json.dumps(row, default=str))
        except:  # pylint: disable=W0702
            log.exception("usage: failed to requeue %s drained event(s)", len(rows))

    @web.method()
    def usage_dequeue_events(self):
        """Up to one batch of queued fact rows, oldest first."""
        redis_config = self.usage_config().get("redis") or {}
        batch = max(1, int(redis_config.get("queue_flush_batch_size", DEFAULT_BATCH_SIZE)))
        #
        try:
            payloads = self.usage_redis_client().lpop(QUEUE_KEY, batch) or []
        except:  # pylint: disable=W0702
            log.exception("usage: failed to read the event queue")
            return []
        #
        rows = []
        #
        for payload in payloads:
            try:
                rows.append(json.loads(payload))
            except (TypeError, ValueError):
                log.error("usage: dropping an unreadable queued event")
        #
        return rows

    @web.method()
    def usage_insert_events(self, connection, rows):
        """Insert the batch and count only what the unique index actually accepted."""
        # The partition key is not nullable and queued rows carry no period of their own
        for row in rows:
            row.setdefault("period", period_of(row["ts"]))
        #
        statement = insert(UsageEvent).values(rows).on_conflict_do_nothing(
            index_elements=["idempotency_key", "ts"],
        ).returning(*RETURNING_COLUMNS)
        #
        landed = [dict(row) for row in connection.execute(statement).mappings()]
        #
        self.usage_apply_counter_deltas(connection, counter_deltas(landed))
        #
        return landed

    @web.method()
    def usage_apply_counter_deltas(self, connection, deltas):
        """ Method """
        for delta in deltas:
            connection.execute(counter_upsert(delta))

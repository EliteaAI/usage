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
from sqlalchemy.exc import CompileError

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db  # pylint: disable=E0401

from ._counters import EVENT_TYPE_LLM, member_key, project_key
from .gate import DEAD_QUEUE_KEY, QUEUE_KEY
from .schema import COST_SPLIT_COLUMNS
from ..models.usage_counter import UsageCounter
from ..models.usage_event import UsageEvent

DEFAULT_BATCH_SIZE = 500

# Batches per tick. Upper bound is set by the drain lease, not by throughput: one tick must
# finish well inside `lease_seconds` (20s) or a second replica starts draining alongside this
# one. LPOP is atomic so no row is processed twice, but do not raise this past 8.
DEFAULT_MAX_BATCHES_PER_TICK = 4
MAX_BATCHES_PER_TICK_CEILING = 8

# A retired row is kept around long enough to be looked at, not forever
DEAD_QUEUE_TTL_SECONDS = 14 * 24 * 3600

# Failures of the row itself, which no amount of retrying can fix. Anything else -- an
# outage above all -- is transient and goes back on the queue untouched.
PERMANENT_ERRORS = (CompileError, TypeError, ValueError)


COUNTER_INDEX = ["project_id", "user_id", "period_kind", "period_start", "model_name"]

# What the counters accumulate; the RETURNING projection is the same list plus the row's identity
MEASURES = ("input_tokens", "output_tokens", "cost_micro_usd")

RETURNING_COLUMNS = (
    UsageEvent.ts, UsageEvent.project_id, UsageEvent.user_id,
    UsageEvent.input_tokens, UsageEvent.output_tokens, UsageEvent.cost_micro_usd,
    UsageEvent.event_type,
)


def event_values(rows):
    """Rows projected onto usage_event's own columns, as copies.

    Copies because a failed tick requeues these same rows: mutating them in place is how a key
    the model no longer has ends up written back into Redis, where it can never be inserted again.
    """
    columns = set(UsageEvent.__table__.columns.keys())
    values = []
    unknown = set()
    #
    for row in rows:
        unknown |= set(row) - columns
        value = {name: item for name, item in row.items() if name in columns}
        # Rows the old code enqueued carry no cost split; they land with zeros rather than
        # failing the whole batch
        for column in COST_SPLIT_COLUMNS:
            value.setdefault(column, 0)
        #
        values.append(value)
    #
    if unknown:
        log.warning(
            "usage: ignoring queued key(s) with no usage_event column: %s", sorted(unknown),
        )
    #
    return values


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
        """One tick: up to N batches, stopping as soon as one comes back short or fails.

        Several batches per tick because a single one caps the drainer at
        batch_size / interval rows per second, which a sustained burst passes and then never
        gives back. If even the full allowance doesn't empty the queue, that already IS the
        signal -- no separate threshold to size or tune.
        """
        redis_config = self.usage_config().get("redis") or {}
        batches = min(MAX_BATCHES_PER_TICK_CEILING, max(1, int(redis_config.get(
            "queue_flush_max_batches_per_tick", DEFAULT_MAX_BATCHES_PER_TICK,
        ))))
        #
        drained = 0
        #
        for _ in range(batches):
            moved = self.usage_drain_one_batch()
            #
            if not moved:
                break
            #
            drained += moved
        else:
            # Every allotted batch came back full: the tick ran out of allowance, not queue
            self.usage_report_backlog(batches)
        #
        return drained

    @web.method()
    def usage_report_backlog(self, batches):
        """Loud, not deduped: called only when a tick used its full batch allowance.

        Checks depth itself rather than trusting the caller's "batches used" count, so a queue
        that happened to empty out on the very last batch stays silent.
        """
        depth = self.usage_queue_depth()
        #
        if depth:
            log.error(
                "usage: drainer fell behind -- %s row(s) still queued after %s batch(es) this "
                "tick; the drainer is not keeping up and this pressures the shared Redis",
                depth, batches,
            )

    @web.method()
    def usage_queue_depth(self):
        """ Method """
        try:
            return int(self.usage_redis_client().llen(QUEUE_KEY))
        except:  # pylint: disable=W0702
            log.exception("usage: failed to read the event queue depth")
            return None

    @web.method()
    def usage_drain_one_batch(self):
        """One batch: queued facts to usage_event, then landed facts to usage_counter."""
        rows = self.usage_dequeue_events()
        #
        if not rows:
            return 0
        #
        try:
            with db.engine.connect() as connection:
                drained = len(self.usage_insert_events(connection, rows))
                connection.commit()
                #
                return drained
        except PERMANENT_ERRORS:
            # Retrying the batch would fail identically forever, so isolate the offending row
            # instead of starving everything queued behind it
            log.exception("usage: drain tick failed to build its insert")
            #
            return self.usage_drain_isolated(rows)
        except:  # pylint: disable=W0702
            log.exception("usage: drain tick failed")
            # Safe to retry because the insert is idempotent on (idempotency_key, ts); dropping
            # the batch instead would lose the facts for good, reconcile included
            self.usage_requeue_events(rows)
            #
            return 0

    @web.method()
    def usage_drain_isolated(self, rows):
        """Second pass after a batch failed to build: each row alone, offenders retired."""
        drained = 0
        #
        for row in rows:
            try:
                with db.engine.connect() as connection:
                    drained += len(self.usage_insert_events(connection, [row]))
                    connection.commit()
            except PERMANENT_ERRORS:
                log.exception("usage: retiring a queued event that cannot be inserted")
                self.usage_retire_events([row])
            except:  # pylint: disable=W0702
                log.exception("usage: a queued event could not be written")
                self.usage_requeue_events([row])
        #
        return drained

    @web.method()
    def usage_retire_events(self, rows):
        """Park unusable rows out of the queue's way, keeping them readable for a while."""
        try:
            client = self.usage_redis_client()
            #
            for row in rows:
                client.rpush(DEAD_QUEUE_KEY, json.dumps(row, default=str))
            #
            client.expire(DEAD_QUEUE_KEY, DEAD_QUEUE_TTL_SECONDS)
        except:  # pylint: disable=W0702
            log.exception("usage: failed to retire %s unusable event(s)", len(rows))

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
        statement = insert(UsageEvent).values(event_values(rows)).on_conflict_do_nothing(
            index_elements=["idempotency_key", "ts"],
        ).returning(*RETURNING_COLUMNS)
        #
        landed = [dict(row) for row in connection.execute(statement).mappings()]
        #
        self.usage_apply_counter_deltas(connection, counter_deltas([
            row for row in landed if row.get("event_type") == EVENT_TYPE_LLM
        ]))
        #
        return landed

    @web.method()
    def usage_apply_counter_deltas(self, connection, deltas):
        """ Method """
        for delta in deltas:
            connection.execute(counter_upsert(delta))

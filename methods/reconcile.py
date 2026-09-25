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

""" Counter reconcile — usage_counter against the usage_event facts it summarises """

import datetime
import json
import time

from sqlalchemy import func, select

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db  # pylint: disable=E0401

from ._counters import (
    ALL_MODELS_SENTINEL, EVENT_TYPE_LLM, PERIOD_MONTH, PROJECT_USER_SENTINEL, period_start,
)
from .drainer import counter_upsert
from .gate import KEY_PREFIX, QUEUE_KEY, member_hash_key, period_of, project_hash_key
from ..models.usage_counter import UsageCounter
from ..models.usage_event import UsageEvent

MEASURES = ("input_tokens", "output_tokens", "cost_nano_usd", "call_count")

DEFAULT_REPAIR_BATCH_SIZE = 500

# Session-scoped, not per-transaction: one connection holds this for the whole run and releases
# it explicitly, so two overlapping runs (a cron double-fire, or cron vs. a manual RPC call)
# never both apply a repair. Arbitrary but must stay stable across deploys — changing it drops
# mutual exclusion with any peer still running the old value.
RECONCILE_ADVISORY_LOCK_KEY = 7965501001

# Gate counters only move at settle, right after the fact is queued; once the drainer has passed a
# cutoff, facts before it and the counter read at it must agree, however busy the project is
DEFAULT_DRAIN_WAIT_SECONDS = 120
DEFAULT_DRAIN_GRACE_SECONDS = 20
DRAIN_POLL_SECONDS = 1

RECONCILE_HISTORY_KEY = f"{KEY_PREFIX}:reconcile:history"
RECONCILE_HISTORY_MAX = 168  # ~1 week of hourly runs
RECONCILE_HISTORY_TTL_SECONDS = 14 * 24 * 3600


def _accumulate(totals, key, measured):
    bucket = totals.setdefault(key, {measure: 0 for measure in MEASURES})
    #
    for measure in MEASURES:
        bucket[measure] += measured[measure]

def _repair_row(row):
    """The delta that closes the gap; the upsert accumulates, so it is a difference."""
    return {
        "project_id": row["project_id"],
        "user_id": row["user_id"],
        "period_kind": PERIOD_MONTH,
        "period_start": row["period_start"],
        "model_name": ALL_MODELS_SENTINEL,
        **{
            measure: row["expected"][measure] - row["actual"][measure]
            for measure in MEASURES
        },
    }



def _repair_hash_key(row):
    """The gate-side counter hash a drift row's correction belongs to."""
    if int(row["user_id"]) == PROJECT_USER_SENTINEL:
        return project_hash_key(row["project_id"], row["period_start"])
    #
    return member_hash_key(row["project_id"], row["user_id"], row["period_start"])


def _redis_counter_owner(hash_key):
    """(project_id, user_id) of a gate counter hash, or None for any other key."""
    parts = str(hash_key).split(":")
    #
    try:
        if len(parts) == 5 and parts[2] == "p":
            return int(parts[3]), PROJECT_USER_SENTINEL
        #
        if len(parts) == 6 and parts[2] == "u":
            return int(parts[3]), int(parts[4])
    except ValueError:
        pass
    #
    return None


def _redis_repair_row(key, start, expected, actual):
    return {
        "project_id": key[0], "user_id": key[1], "period_start": start.date(),
        "expected": {"cost_nano_usd": expected}, "actual": {"cost_nano_usd": actual},
    }


def _queued_ts(payload):
    try:
        return datetime.datetime.fromisoformat(str(json.loads(payload)["ts"]))
    except (TypeError, ValueError, KeyError):
        return None


def period_bounds(period):
    """[start, next_start) of a 'YYYYMM' period, as UTC datetimes."""
    year, month = int(str(period)[:4]), int(str(period)[4:6])
    start = datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc)
    #
    if month == 12:
        return start, datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    #
    return start, datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc)


def current_period():
    """ Helper """
    return f"{datetime.datetime.now(datetime.timezone.utc):%Y%m}"


def _reconcile_report(self, period, start, end):
    """Report-only path: read-only, so it needs no lock and can run alongside anything."""
    try:
        with db.engine.connect() as connection:
            drift = self.usage_counter_drift(connection, start, end)
    except:  # pylint: disable=W0702
        log.exception("usage: reconcile failed to compute drift for period %s", period)
        return {"period": period, "applied": False, "drift": None}
    #
    if drift:
        log.warning("usage: %s counter row(s) drift in period %s", len(drift), period)
    #
    redis_drift = _redis_drift_or_none(self, period, start, end)
    #
    return {"period": period, "applied": False, "drift": drift, "redis_drift": redis_drift}


def _redis_drift_or_none(self, period, start, end):
    """None when the gate side could not be judged this run; it is retried on the next one."""
    try:
        redis_drift = self.usage_redis_counter_drift(start, end)
    except Exception:  # pylint: disable=W0703
        # Not bare except: this wraps a blocking drain-wait loop, and on gevent a bare except
        # also swallows cooperative-cancellation exceptions (GreenletExit/Timeout), not just errors
        log.exception("usage: reconcile failed to compare gate counters for period %s", period)
        return None
    #
    if redis_drift:
        log.warning(
            "usage: %s gate counter(s) in Redis drift from the facts in period %s: %s",
            len(redis_drift), period, redis_drift,
        )
    #
    return redis_drift


def _reconcile_apply(self, period, start, end):
    """Apply path: called only while the caller holds the reconcile advisory lock."""
    try:
        with db.engine.connect() as connection:
            drift = self.usage_counter_drift(connection, start, end)
    except:  # pylint: disable=W0702
        log.exception("usage: reconcile failed to compute drift for period %s", period)
        return {"period": period, "applied": False, "drift": None}
    #
    repaired, failed = self.usage_reconcile_repair(drift) if drift else (0, [])
    #
    if drift:
        log.warning("usage: %s counter row(s) drift in period %s", len(drift), period)
    #
    if failed:
        # Loud and not deduped: each failed row is named individually so a log aggregator
        # that collapses identical messages still shows every one of them
        log.error(
            "usage: %s of %s drift repair(s) FAILED for period %s — counters left "
            "unrepaired (not partially written): %s",
            len(failed), len(drift), period, failed,
        )
    #
    # After the Postgres repair, which pushes its own deltas into Redis: measured first, the
    # same gap would be closed twice
    redis_drift = _redis_drift_or_none(self, period, start, end)
    redis_repaired = self.usage_reconcile_push_repairs(redis_drift) if redis_drift else 0
    #
    self.usage_reconcile_record_run({
        "period": period,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "drift": len(drift),
        "repaired": repaired,
        "failed": len(failed),
        "redis_drift": None if redis_drift is None else len(redis_drift),
        "redis_repaired": redis_repaired,
        "queue_depth": self.usage_queue_depth(),
    })
    #
    return {
        "period": period, "applied": not failed, "drift": drift,
        "repaired": repaired, "failed": failed,
        "redis_drift": redis_drift, "redis_repaired": redis_repaired,
    }


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_reconcile_counters(self, period=None, apply=False):  # pylint: disable=W0622
        """Drift between the counters and the facts. Reports only unless apply is set.

        A report-only run never writes, so it is always safe to run alongside anything else.
        An apply run takes the advisory lock first: skipped (not queued, not retried) if another
        apply run already holds it, so a cron double-fire or a manual usage_reconcile_counters_now
        call can never both repair the same drift.
        """
        period = str(period or current_period())
        start, end = period_bounds(period)
        #
        if not apply:
            return _reconcile_report(self, period, start, end)
        #
        with db.engine.connect() as lock_connection:
            if not self.usage_reconcile_lock(lock_connection):
                log.warning(
                    "usage: reconcile for period %s skipped — another apply run holds the lock",
                    period,
                )
                return {"period": period, "applied": False, "drift": None, "skipped": "locked"}
            #
            try:
                return _reconcile_apply(self, period, start, end)
            finally:
                self.usage_reconcile_unlock(lock_connection)

    @web.method()
    def usage_reconcile_lock(self, connection):
        """True if this connection now holds the reconcile lock; false if another one does."""
        try:
            return bool(
                connection.execute(select(func.pg_try_advisory_lock(
                    RECONCILE_ADVISORY_LOCK_KEY,
                ))).scalar(),
            )
        except:  # pylint: disable=W0702
            log.exception("usage: reconcile lock is unreachable; refusing to run unlocked")
            return False

    @web.method()
    def usage_reconcile_unlock(self, connection):
        """Best-effort: a lock this call never actually holds is simply a no-op release."""
        try:
            connection.execute(select(func.pg_advisory_unlock(RECONCILE_ADVISORY_LOCK_KEY)))
        except:  # pylint: disable=W0702
            log.exception("usage: failed to release the reconcile advisory lock")

    @web.method()
    def usage_reconcile_repair(self, drift):
        """Apply repair deltas in bounded batches.

        Each batch is its own connection and transaction, so one bad batch cannot undo (or block
        the commit of) an already-successful one, and no single transaction holds locks over the
        whole drift set at 20k-project scale. A batch that fails to apply is retried row by row so
        one bad row cannot block the rest of it, mirroring the drainer's isolate-the-offender
        fallback; a row that still fails is left untouched (counter_upsert is one statement, so
        there is no partial write) and reported in `failed`.
        """
        batch_size = max(1, int((self.usage_config().get("reconcile") or {}).get(
            "repair_batch_size", DEFAULT_REPAIR_BATCH_SIZE,
        )))
        repaired, failed = 0, []
        #
        for offset in range(0, len(drift), batch_size):
            batch = drift[offset:offset + batch_size]
            # Collected, then pushed outside the try: a push failure must not re-enter the
            # retry path and apply the same Postgres delta twice
            pushed = []
            #
            try:
                with db.engine.connect() as connection:
                    for row in batch:
                        connection.execute(counter_upsert(_repair_row(row)))
                    #
                    connection.commit()
                #
                repaired += len(batch)
                pushed = batch
            except:  # pylint: disable=W0702
                log.exception(
                    "usage: repair batch of %s row(s) failed to apply; retrying individually",
                    len(batch),
                )
                #
                for row in batch:
                    try:
                        with db.engine.connect() as connection:
                            connection.execute(counter_upsert(_repair_row(row)))
                            connection.commit()
                        #
                        repaired += 1
                        pushed.append(row)
                    except:  # pylint: disable=W0702
                        log.error(
                            "usage: repair FAILED for project %s user %s period %s — "
                            "counter left unrepaired, no partial write",
                            row["project_id"], row["user_id"], row["period_start"],
                        )
                        failed.append({
                            "project_id": row["project_id"], "user_id": row["user_id"],
                        })
            #
            self.usage_reconcile_push_repairs(pushed)
        #
        return repaired, failed

    @web.method()
    def usage_reconcile_push_repairs(self, rows):
        """Mirror repaired rows into the gate's Redis counters — the layer enforcement reads.

        Postgres-only repair would leave the gate blocking against the stale, higher figure.
        Whole batch in one call, so the push costs one round trip rather than one per row.
        """
        return self.usage_gate_push_counter_deltas([
            (
                _repair_hash_key(row),
                row["expected"]["cost_nano_usd"] - row["actual"]["cost_nano_usd"],
            )
            for row in rows
        ])

    @web.method()
    def usage_reconcile_record_run(self, summary):
        """Append to a rolling history in Redis, so drift stays visible past the log line and
        past an apply run that repaired it — the only way to notice a pattern of recurring drift
        once auto-repair stops requiring a human to look."""
        try:
            client = self.usage_redis_client()
            client.rpush(RECONCILE_HISTORY_KEY, json.dumps(summary, default=str))
            client.ltrim(RECONCILE_HISTORY_KEY, -RECONCILE_HISTORY_MAX, -1)
            client.expire(RECONCILE_HISTORY_KEY, RECONCILE_HISTORY_TTL_SECONDS)
        except:  # pylint: disable=W0702
            log.exception("usage: failed to record reconcile run history")

    @web.method()
    def usage_reconcile_history(self, limit=RECONCILE_HISTORY_MAX):
        """The most recent apply-run summaries, oldest first."""
        try:
            raw = self.usage_redis_client().lrange(RECONCILE_HISTORY_KEY, -max(1, int(limit)), -1)
        except:  # pylint: disable=W0702
            log.exception("usage: failed to read reconcile run history")
            return []
        #
        history = []
        #
        for item in raw:
            try:
                history.append(json.loads(item))
            except (TypeError, ValueError):
                continue
        #
        return history

    @web.method()
    def usage_counter_drift(self, connection, start, end):
        """Rows where the counters and the facts disagree, facts taken as the truth."""
        facts = self.usage_fact_totals(connection, start, end)
        counters = self.usage_counter_totals(connection, start.date())
        #
        drift = []
        #
        # Union of both sides: a counter row with no facts behind it is drift too, and
        # iterating the facts alone would leave it inflated forever
        for key in set(facts) | set(counters):
            zero = {measure: 0 for measure in MEASURES}
            fact = facts.get(key) or zero
            counter = counters.get(key) or zero
            #
            if any(fact[measure] != counter[measure] for measure in MEASURES):
                drift.append({
                    "project_id": key[0], "user_id": key[1], "period_start": start.date(),
                    "expected": fact, "actual": counter,
                })
        #
        return drift

    @web.method()
    def usage_redis_counter_drift(self, start, end):
        """Gate counters in Redis that disagree with the facts, confirmed by two samples.

        A standing gap reads the same both times; a call settling across a sample boundary
        does not, so only an identical delta is reported and repaired.
        """
        first = self.usage_redis_drift_sample(start, end)
        #
        if not first:
            return None if first is None else []
        #
        second = self.usage_redis_drift_sample(start, end)
        #
        if second is None:
            return None
        #
        return [
            _redis_repair_row(key, start, *second[key])
            for key in sorted(first)
            if key in second and first[key][0] - first[key][1] == second[key][0] - second[key][1]
        ]

    @web.method()
    def usage_redis_drift_sample(self, start, end):
        """{key: (facts, redis)} where they differ, or None when the drainer is too far behind."""
        live = self.usage_redis_counter_totals(start)
        #
        if not live:
            return {}
        # Taken after the read: every settle Redis has seen was queued before this instant
        cutoff = datetime.datetime.now(datetime.timezone.utc)
        #
        if not self.usage_wait_drained_past(cutoff):
            log.warning("usage: drainer is behind; gate counters not compared this run")
            return None
        #
        with db.engine.connect() as connection:
            facts = self.usage_fact_totals(connection, start, min(end, cutoff))
        #
        sample = {}
        #
        # Live keys only: a cold one is primed from the repaired Postgres row on its next call
        for key, actual in live.items():
            expected = (facts.get(key) or {}).get("cost_nano_usd", 0)
            #
            if expected != actual:
                sample[key] = (expected, actual)
        #
        return sample

    @web.method()
    def usage_redis_counter_totals(self, start):
        """{(project_id, user_id): counter} for every live gate hash of the period."""
        client = self.usage_redis_client()
        keys = [
            key for key in client.scan_iter(
                match=f"{KEY_PREFIX}:ctr:*:{period_of(start)}", count=1000,
            )
            if _redis_counter_owner(key) is not None
        ]
        #
        if not keys:
            return {}
        #
        pipe = client.pipeline(transaction=False)
        #
        for key in keys:
            pipe.hget(key, "counter")
        #
        return {
            _redis_counter_owner(key): int(counter)
            for key, counter in zip(keys, pipe.execute())
            if counter is not None
        }

    @web.method()
    def usage_wait_drained_past(self, cutoff):
        """True once no fact from before the cutoff can still be queued or mid-insert."""
        redis_config = self.usage_config().get("redis") or {}
        grace = int(redis_config.get("lease_seconds", DEFAULT_DRAIN_GRACE_SECONDS))
        deadline = time.monotonic() + DEFAULT_DRAIN_WAIT_SECONDS
        client = self.usage_redis_client()
        #
        while True:
            head = client.lindex(QUEUE_KEY, 0)
            head_ts = None if head is None else _queued_ts(head)
            #
            if head is None or (head_ts is not None and head_ts >= cutoff):
                break
            #
            if time.monotonic() >= deadline:
                return False
            #
            time.sleep(DRAIN_POLL_SECONDS)
        #
        # A batch popped before the cutoff commits within one drain tick, bounded by the lease
        time.sleep(max(0, grace))
        #
        return True

    @web.method()
    def usage_fact_totals(self, connection, start, end):
        """Per-project and per-member sums straight off usage_event.

        LLM rows only: nothing counts other event types into usage_counter, so including them
        here would report drift that is by design.
        """
        statement = select(
            UsageEvent.project_id, UsageEvent.user_id,
            func.sum(UsageEvent.input_tokens), func.sum(UsageEvent.output_tokens),
            func.sum(UsageEvent.cost_nano_usd), func.count(),
        ).where(
            UsageEvent.ts >= start, UsageEvent.ts < end, UsageEvent.event_type == EVENT_TYPE_LLM,
        ).group_by(
            UsageEvent.project_id, UsageEvent.user_id,
        )
        #
        totals = {}
        #
        for project_id, user_id, inputs, outputs, cost, calls in connection.execute(statement):
            measured = {
                "input_tokens": int(inputs or 0), "output_tokens": int(outputs or 0),
                "cost_nano_usd": int(cost or 0), "call_count": int(calls or 0),
            }
            #
            _accumulate(totals, (project_id, PROJECT_USER_SENTINEL), measured)
            #
            if user_id:
                _accumulate(totals, (project_id, user_id), measured)
        #
        return totals

    @web.method()
    def usage_counter_totals(self, connection, period_day):
        """The aggregate counter rows for the period, keyed the same way as the fact totals."""
        statement = select(
            UsageCounter.project_id, UsageCounter.user_id,
            UsageCounter.input_tokens, UsageCounter.output_tokens,
            UsageCounter.cost_nano_usd, UsageCounter.call_count,
        ).where(
            UsageCounter.period_kind == PERIOD_MONTH,
            UsageCounter.period_start == period_day,
            UsageCounter.model_name == ALL_MODELS_SENTINEL,
        )
        #
        return {
            (project_id, user_id): {
                "input_tokens": int(inputs or 0), "output_tokens": int(outputs or 0),
                "cost_nano_usd": int(cost or 0), "call_count": int(calls or 0),
            }
            for project_id, user_id, inputs, outputs, cost, calls
            in connection.execute(statement)
        }

    @web.method()
    def usage_reconcile_period_start(self, period):
        """ Method """
        return period_start(period_bounds(period)[0], PERIOD_MONTH)

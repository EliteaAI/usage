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

""" Complexity jobs: hourly scoring, nightly calibration + retention, backfill (#7553)

Greenlets on a single-CPU pylon: aggregation stays in SQL, Python scores small chunks and yields. """

import datetime
import json
import threading
import time
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db, config as c  # pylint: disable=E0401

from . import _complexity as cx
from .mode import MODE_ENFORCE, MODE_OBSERVE
from .partitions import (
    month_range, next_month, partition_name, partition_statements, relation_exists,
)
from ..models.entity_complexity import (
    EntityComplexityDaily, EntityComplexityRun, EntityComplexityState,
)

COMPLEXITY_ADVISORY_LOCK_KEY = 7965501002
RUN_PARENT = "entity_complexity_run"
STATE_ID = 1

DEFAULT_QUIET_MINUTES = 30
# ~26 binds per row: 250 rows stays far below the 32k driver limit
CHUNK_SIZE = 250
WINDOW = datetime.timedelta(days=1)
# Hourly pass stops after this much wall time; the next pass resumes from the watermark
HOURLY_BUDGET_SECONDS = 300
FIRST_PASS_LOOKBACK = datetime.timedelta(days=1)
STATEMENT_TIMEOUT_MS = 120000
DDL_LOCK_TIMEOUT_MS = 5000
YIELD_SECONDS = 0.05
DEFAULT_RUN_RETENTION_DAYS = 90
DEFAULT_DAILY_RETENTION_MONTHS = 13
MAX_BACKFILL_DAYS = 90
CALIBRATION_WINDOW_DAYS = 30
CALIBRATION_RUNS_PER_ENTITY = 50
ENTITY_LABEL_MIN_RUNS = 3
DAILY_DELETE_BATCH = 5000

# Embedding calls carry a run_id from RAG tools but are not reasoning steps
_LLM_FILTER = (
    "e.event_type = 'llm' AND NOT e.is_error "
    "AND COALESCE(e.endpoint, '') !~ '/(embeddings|embed)/?$'"
)
_SCORABLE = (
    "e.run_id IS NOT NULL AND e.root_entity_id IS NOT NULL "
    "AND e.event_type IN ('llm', 'tool')"
)

# Per-project probe on ix_usage_event_project_ts: bounded by the window, never a month scan
WINDOW_RUNS_SQL = f"""
SELECT DISTINCT w.project_id, w.run_id
FROM {c.POSTGRES_SCHEMA}.project p
CROSS JOIN LATERAL (
    SELECT e.project_id, e.run_id FROM {c.POSTGRES_SCHEMA}.usage_event e
    WHERE e.project_id = p.id AND e.ts > :start AND e.ts <= :end AND {_SCORABLE}
) w
"""

# Whole run, no lookback: a run's first-event ts (part of its key) must never shift.
# LATERAL forces one ix_usage_event_project_run probe per run instead of a table-wide hash join.
RUNS_SQL = f"""
SELECT k.project_id, k.run_id, min(e.ts) AS ts, max(e.ts) AS last_event_at,
       max(e.conversation_id) AS conversation_id, max(e.trigger_source) AS trigger_source,
       max(e.root_entity_type) AS root_entity_type, max(e.root_entity_id) AS root_entity_id,
       max(e.root_entity_version_id) AS root_entity_version_id,
       COALESCE(max(e.root_entity_project_id), k.project_id) AS root_entity_project_id,
       COALESCE(max(e.input_tokens) FILTER (WHERE {_LLM_FILTER}), 0) AS peak_input_tokens,
       count(*) FILTER (WHERE {_LLM_FILTER}) AS llm_calls,
       count(*) FILTER (WHERE e.event_type = 'tool') AS tool_calls
FROM unnest(CAST(:project_ids AS bigint[]), CAST(:run_ids AS text[])) AS k(project_id, run_id)
CROSS JOIN LATERAL (
    SELECT * FROM {c.POSTGRES_SCHEMA}.usage_event e
    WHERE e.project_id = k.project_id AND e.run_id = k.run_id AND {_SCORABLE}
) e
GROUP BY k.project_id, k.run_id
"""

TOOLS_SQL = f"""
SELECT k.project_id, k.run_id, e.tool_name, e.meta ->> 'toolkit_type' AS toolkit_type,
       count(*) AS calls
FROM unnest(CAST(:project_ids AS bigint[]), CAST(:run_ids AS text[])) AS k(project_id, run_id)
CROSS JOIN LATERAL (
    SELECT e.tool_name, e.meta FROM {c.POSTGRES_SCHEMA}.usage_event e
    WHERE e.project_id = k.project_id AND e.run_id = k.run_id
      AND {_SCORABLE} AND e.event_type = 'tool'
) e
GROUP BY k.project_id, k.run_id, e.tool_name, e.meta ->> 'toolkit_type'
"""

DAILY_UPSERT_SQL = f"""
INSERT INTO {c.POSTGRES_SCHEMA}.entity_complexity_daily AS d (
    project_id, root_entity_project_id, root_entity_type, root_entity_id, day,
    runs, peak_input_tokens_sum, llm_calls, tool_calls,
    cu_context_sum, cu_llm_sum, cu_tools_sum, cu_total_sum, cu_max, cu_histogram, updated_at
)
SELECT k.project_id, k.root_entity_project_id, k.root_entity_type, k.root_entity_id, k.day,
       agg.runs, agg.peak, agg.llm, agg.tools, agg.cu_context, agg.cu_llm, agg.cu_tools,
       agg.cu_total, agg.cu_max, agg.histogram, now()
FROM unnest(
    CAST(:project_ids AS bigint[]), CAST(:entity_project_ids AS bigint[]),
    CAST(:types AS text[]), CAST(:entity_ids AS bigint[]), CAST(:days AS date[])
) AS k(project_id, root_entity_project_id, root_entity_type, root_entity_id, day)
CROSS JOIN LATERAL (
    SELECT count(*) AS runs, sum(r.peak_input_tokens) AS peak,
           sum(r.llm_calls) AS llm, sum(r.tool_calls) AS tools,
           round(sum(r.cu_context)::numeric, 3) AS cu_context,
           round(sum(r.cu_llm)::numeric, 3) AS cu_llm,
           round(sum(r.cu_tools)::numeric, 3) AS cu_tools,
           round(sum(r.cu_total)::numeric, 3) AS cu_total, max(r.cu_total) AS cu_max,
           ARRAY(
               SELECT count(r2.bucket) FROM generate_series(0, {cx.HISTOGRAM_BUCKETS - 1}) AS b(i)
               LEFT JOIN unnest(array_agg(
                   LEAST(CASE WHEN r.cu_total < 1 THEN 0
                         ELSE floor(log(2, r.cu_total::numeric))::int + 1 END,
                         {cx.HISTOGRAM_BUCKETS - 1})
               )) AS r2(bucket) ON r2.bucket = b.i
               GROUP BY b.i ORDER BY b.i
           ) AS histogram
    FROM {c.POSTGRES_SCHEMA}.{RUN_PARENT} r
    WHERE r.project_id = k.project_id AND r.root_entity_project_id = k.root_entity_project_id
      AND r.root_entity_type = k.root_entity_type AND r.root_entity_id = k.root_entity_id
      AND r.ts >= (k.day::timestamp AT TIME ZONE 'UTC')
      AND r.ts < ((k.day + 1)::timestamp AT TIME ZONE 'UTC')
) agg
WHERE agg.runs > 0
ON CONFLICT (project_id, root_entity_project_id, root_entity_type, root_entity_id, day)
DO UPDATE SET runs = EXCLUDED.runs, peak_input_tokens_sum = EXCLUDED.peak_input_tokens_sum,
    llm_calls = EXCLUDED.llm_calls, tool_calls = EXCLUDED.tool_calls,
    cu_context_sum = EXCLUDED.cu_context_sum, cu_llm_sum = EXCLUDED.cu_llm_sum,
    cu_tools_sum = EXCLUDED.cu_tools_sum, cu_total_sum = EXCLUDED.cu_total_sum,
    cu_max = EXCLUDED.cu_max, cu_histogram = EXCLUDED.cu_histogram, updated_at = now()
"""

# Everything statistical happens here: Python only receives a few small arrays
CALIBRATION_SQL = f"""
WITH sample AS (
    SELECT root_entity_project_id, root_entity_type, root_entity_id, cu_total FROM (
        SELECT root_entity_project_id, root_entity_type, root_entity_id, cu_total,
               row_number() OVER (
                   PARTITION BY root_entity_project_id, root_entity_type, root_entity_id
                   ORDER BY ts DESC
               ) AS rn
        FROM {c.POSTGRES_SCHEMA}.{RUN_PARENT}
        WHERE ts >= :since
    ) ranked WHERE rn <= :per_entity
), medians AS (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY cu_total) AS cu
    FROM sample GROUP BY root_entity_project_id, root_entity_type, root_entity_id
    HAVING count(*) >= :entity_min_runs
)
SELECT
    (SELECT count(*) FROM sample) AS sample_runs,
    (SELECT count(*) FROM (
        SELECT DISTINCT root_entity_project_id, root_entity_type, root_entity_id FROM sample
    ) entities) AS sample_entities,
    (SELECT percentile_cont(CAST(:quantiles AS float8[])) WITHIN GROUP (ORDER BY cu_total)
     FROM sample) AS calibration,
    (SELECT percentile_cont(CAST(:breakpoints AS float8[])) WITHIN GROUP (ORDER BY cu_total)
     FROM sample) AS run_percentiles,
    (SELECT percentile_cont(CAST(:breakpoints AS float8[])) WITHIN GROUP (ORDER BY cu)
     FROM medians) AS entity_percentiles
"""

RUN_PARTITIONS_SQL = f"""
SELECT child.relname FROM pg_inherits i
JOIN pg_class parent ON parent.oid = i.inhparent
JOIN pg_class child ON child.oid = i.inhrelid
JOIN pg_namespace n ON n.oid = parent.relnamespace
WHERE n.nspname = '{c.POSTGRES_SCHEMA}' AND parent.relname = '{RUN_PARENT}'
"""

DAILY_RETENTION_SQL = f"""
DELETE FROM {c.POSTGRES_SCHEMA}.entity_complexity_daily
WHERE ctid IN (
    SELECT ctid FROM {c.POSTGRES_SCHEMA}.entity_complexity_daily
    WHERE day < :before LIMIT :limit
)
"""

BREAKPOINTS = [p / 100.0 for p in range(1, 100)]


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _yield():
    # Under gevent this hands the single CPU back to request greenlets between chunks
    time.sleep(YIELD_SECONDS)


def _subtract_months(day, months):
    year, month = day.year, day.month - months
    while month <= 0:
        year, month = year - 1, month + 12
    return datetime.date(year, month, 1)


def _round_list(values):
    return [round(float(v), 3) for v in values] if values else None


def ensure_run_partitions(connection, months):
    """Create missing monthly run partitions; False when the parent table is absent."""
    if not relation_exists(connection, c.POSTGRES_SCHEMA, RUN_PARENT, relkinds=("p",)):
        log.warning(
            "usage: %s.%s is missing; run the admin create_tables task",
            c.POSTGRES_SCHEMA, RUN_PARENT,
        )
        return False
    missing = [
        (year, month) for year, month in months
        if not relation_exists(
            connection, c.POSTGRES_SCHEMA, partition_name(year, month, RUN_PARENT), relkinds=("r",),
        )
    ]
    # CREATE ... PARTITION OF locks the parent: give up fast rather than queue readers behind it
    for year, month in missing:
        connection.execute(text(f"SET LOCAL lock_timeout = {DDL_LOCK_TIMEOUT_MS}"))
        for statement in partition_statements(
                c.POSTGRES_SCHEMA, (year, month), (year, month), RUN_PARENT,
        ):
            connection.execute(text(statement))
        connection.commit()
    return True


def expired_run_partitions(names, today, retention_days):
    """Run partitions whose whole month ended before the retention horizon."""
    horizon = today - datetime.timedelta(days=retention_days)
    expired = []
    prefix = f"{RUN_PARENT}_"
    for name in names:
        suffix = name[len(prefix):] if name.startswith(prefix) else ""
        if len(suffix) != 6 or not suffix.isdigit():
            continue
        end_year, end_month = next_month(int(suffix[:4]), int(suffix[4:]))
        if datetime.date(end_year, end_month, 1) <= horizon:
            expired.append(name)
    return sorted(expired)


def build_run_rows(run_rows, tool_rows, weights, scored_at):
    """Join per-run aggregates with per-tool counts and score them (rows with no work skipped)."""
    tools = defaultdict(list)
    for row in tool_rows:
        tools[(row["project_id"], row["run_id"])].append(
            (row["tool_name"], row["toolkit_type"], row["calls"]),
        )
    result = []
    for row in run_rows:
        if not row["llm_calls"] and not row["tool_calls"]:
            continue
        counts = tools.get((row["project_id"], row["run_id"]), [])
        score = cx.score_run(row["peak_input_tokens"], row["llm_calls"], counts, weights)
        tool_counts = defaultdict(int)
        for tool_name, toolkit_type, calls in counts:
            label = f"{toolkit_type}:{tool_name}" if toolkit_type else str(tool_name)
            tool_counts[label] += int(calls)
        result.append({
            "project_id": row["project_id"], "run_id": row["run_id"],
            "ts": row["ts"], "last_event_at": row["last_event_at"],
            "conversation_id": row["conversation_id"], "trigger_source": row["trigger_source"],
            "root_entity_type": row["root_entity_type"], "root_entity_id": row["root_entity_id"],
            "root_entity_version_id": row["root_entity_version_id"],
            "root_entity_project_id": row["root_entity_project_id"],
            "peak_input_tokens": int(row["peak_input_tokens"] or 0),
            "llm_calls": int(row["llm_calls"] or 0), "tool_calls": int(row["tool_calls"] or 0),
            "tool_counts": dict(tool_counts) or None,
            **score,
            "formula_version": cx.FORMULA_VERSION,
            "tool_weights_version": weights["version"],
            "scored_at": scored_at,
        })
    return result


def daily_keys(run_rows):
    """Distinct (project, entity project, entity type, entity id, UTC day) of a set of runs."""
    keys = set()
    for row in run_rows:
        day = row["ts"].astimezone(datetime.timezone.utc).date()
        keys.add((
            row["project_id"], row["root_entity_project_id"],
            row["root_entity_type"], row["root_entity_id"], day,
        ))
    return sorted(keys)


def windows(start, end, step=WINDOW):
    """Half-open (lo, hi] windows of at most step covering (start, end]."""
    result = []
    lo = start
    while lo < end:
        hi = min(lo + step, end)
        result.append((lo, hi))
        lo = hi
    return result


def calibration_summary(row, config):
    """Thresholds plus percentiles from the one-row SQL calibration result."""
    sample_runs = int(row["sample_runs"] or 0)
    sample_entities = int(row["sample_entities"] or 0)
    thresholds, source = cx.resolve_thresholds(
        row["calibration"], sample_runs, sample_entities, config,
    )
    return {
        "thresholds": thresholds,
        "thresholds_source": source,
        "run_percentiles": _round_list(row["run_percentiles"]),
        "entity_percentiles": _round_list(row["entity_percentiles"]),
        "sample_runs": sample_runs,
        "sample_entities": sample_entities,
    }


def _parse_days(param, default):
    """Admin task param: bare `N` or `days=N`, clamped to 1..90."""
    value = str(param or "").strip()
    if value.startswith("days="):
        value = value[len("days="):]
    try:
        return max(1, min(int(value), MAX_BACKFILL_DAYS)) if value else default
    except ValueError:
        return default


def _spawn(name, target):
    threading.Thread(target=target, name=name, daemon=True).start()


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_complexity_config(self):
        """The usage.complexity block of the plugin config."""
        return self.usage_config().get("complexity", None) or {}

    @web.method()
    def usage_complexity_is_enabled(self):
        """Switch on AND Cost Budgets metering on; without metering there is nothing to score."""
        if not bool(self.usage_complexity_config().get("enabled", False)):
            return False
        return self.usage_get_mode() in (MODE_OBSERVE, MODE_ENFORCE)

    @web.method()
    def usage_complexity_locked(self, job):
        """job(connection) on one connection holding the advisory lock; None when it is taken."""
        connection = db.engine.connect()
        locked = False
        try:
            locked = bool(connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": COMPLEXITY_ADVISORY_LOCK_KEY},
            ).scalar())
            # The session lock survives commit; committing keeps the connection off idle-in-txn
            connection.commit()
            if not locked:
                log.info("usage: complexity job skipped, another one is running")
                return None
            connection.execute(text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            connection.commit()
            return job(connection)
        finally:
            self.usage_complexity_release(connection, locked)

    @web.method()
    def usage_complexity_release(self, connection, locked):
        """Unlock and return the connection; one we cannot clean is discarded, never pooled."""
        try:
            connection.rollback()
            if locked:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": COMPLEXITY_ADVISORY_LOCK_KEY},
                )
            connection.execute(text("RESET statement_timeout"))
            connection.commit()
        except:  # pylint: disable=W0702
            log.exception("usage: complexity lock release failed; discarding the connection")
            connection.invalidate()
        finally:
            connection.close()

    @web.method()
    def usage_complexity_state(self, connection):
        """The single state row as a dict, created empty on first use."""
        table = EntityComplexityState.__table__
        connection.execute(
            insert(table).values(id=STATE_ID).on_conflict_do_nothing(index_elements=["id"]),
        )
        row = connection.execute(table.select().where(table.c.id == STATE_ID)).mappings().first()
        connection.commit()
        return dict(row)

    @web.method()
    def usage_complexity_set_state(self, connection, **values):
        """Update the single state row and commit."""
        table = EntityComplexityState.__table__
        connection.execute(table.update().where(table.c.id == STATE_ID).values(**values))
        connection.commit()

    @web.method()
    def usage_complexity_score_window(self, connection, start, end):
        """Score every run with activity in (start, end]; returns runs scored."""
        keys = connection.execute(
            text(WINDOW_RUNS_SQL), {"start": start, "end": end},
        ).all()
        connection.commit()
        scored = 0
        weights = cx.load_tool_weights()
        for offset in range(0, len(keys), CHUNK_SIZE):
            chunk = keys[offset:offset + CHUNK_SIZE]
            params = {
                "project_ids": [key[0] for key in chunk], "run_ids": [key[1] for key in chunk],
            }
            run_rows = connection.execute(text(RUNS_SQL), params).mappings().all()
            tool_rows = connection.execute(text(TOOLS_SQL), params).mappings().all()
            # Still active past this window: the window that holds its last event scores it
            run_rows = [row for row in run_rows if row["last_event_at"] <= end]
            rows = build_run_rows(run_rows, tool_rows, weights, _utcnow())
            if rows:
                months = sorted({(row["ts"].year, row["ts"].month) for row in rows})
                if not ensure_run_partitions(connection, months):
                    connection.rollback()
                    return None
                table = EntityComplexityRun.__table__
                statement = insert(table).values(rows)
                updatable = [k for k in rows[0] if k not in ("project_id", "run_id", "ts")]
                connection.execute(statement.on_conflict_do_update(
                    index_elements=["project_id", "run_id", "ts"],
                    set_={k: statement.excluded[k] for k in updatable},
                ))
                self.usage_complexity_rebuild_daily(connection, daily_keys(rows))
            connection.commit()
            scored += len(rows)
            _yield()
        return scored

    @web.method()
    def usage_complexity_rebuild_daily(self, connection, keys):
        """Recompute the daily rows for the given keys from run rows, in SQL."""
        if not keys:
            return
        connection.execute(text(DAILY_UPSERT_SQL), {
            "project_ids": [k[0] for k in keys], "entity_project_ids": [k[1] for k in keys],
            "types": [k[2] for k in keys], "entity_ids": [k[3] for k in keys],
            "days": [k[4] for k in keys],
        })

    @web.method()
    def usage_complexity_score_range(self, connection, start, end, budget_seconds=None):
        """Score (start, end] a window at a time; returns (scored, watermark reached)."""
        started = time.monotonic()
        scored, reached = 0, start
        for lo, hi in windows(start, end):
            # Switched off mid-pass: stop at a window boundary, the watermark stays consistent
            if not self.usage_complexity_is_enabled():
                break
            count = self.usage_complexity_score_window(connection, lo, hi)
            if count is None:
                break
            scored, reached = scored + count, hi
            if budget_seconds is not None and time.monotonic() - started > budget_seconds:
                break
        return scored, reached

    @web.method()
    def usage_complexity_score(self):
        """Hourly: score runs that went quiet since the watermark, within a time budget."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        quiet = int(self.usage_complexity_config().get("quiet_minutes", DEFAULT_QUIET_MINUTES))
        #
        def job(connection):
            cutoff = _utcnow() - datetime.timedelta(minutes=quiet)
            state = self.usage_complexity_state(connection)
            start = state["scored_through"] or cutoff - FIRST_PASS_LOOKBACK
            scored, reached = self.usage_complexity_score_range(
                connection, start, cutoff, budget_seconds=HOURLY_BUDGET_SECONDS,
            )
            if reached > start:
                self.usage_complexity_set_state(
                    connection, scored_through=reached, last_scored_at=_utcnow(),
                )
            log.info("usage: complexity scored %s run(s) through %s", scored, reached)
            return {"scored": scored, "scored_through": reached.isoformat()}
        #
        return self.usage_complexity_locked(job)

    @web.method()
    def usage_complexity_calibrate(self):
        """Thresholds, percentiles and computed_at from the last 30 days of runs."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        return self.usage_complexity_locked(self.usage_complexity_calibrate_locked)

    @web.method()
    def usage_complexity_calibrate_locked(self, connection):
        """Calibration body; the caller holds the lock."""
        self.usage_complexity_state(connection)
        row = connection.execute(text(CALIBRATION_SQL), {
            "since": _utcnow() - datetime.timedelta(days=CALIBRATION_WINDOW_DAYS),
            "per_entity": CALIBRATION_RUNS_PER_ENTITY,
            "entity_min_runs": ENTITY_LABEL_MIN_RUNS,
            "quantiles": list(cx.CALIBRATION_QUANTILES),
            "breakpoints": BREAKPOINTS,
        }).mappings().first()
        connection.commit()
        summary = calibration_summary(row, self.usage_complexity_config())
        self.usage_complexity_set_state(connection, computed_at=_utcnow(), **summary)
        log.info(
            "usage: complexity thresholds %s (%s) from %s run(s)",
            json.dumps(summary["thresholds"]), summary["thresholds_source"], summary["sample_runs"],
        )
        return summary

    @web.method()
    def usage_complexity_retention(self, connection, today=None):
        """Drop run partitions past retention and batch-delete old daily rows."""
        config = self.usage_complexity_config()
        today = today or _utcnow().date()
        run_days = int(config.get("run_retention_days", DEFAULT_RUN_RETENTION_DAYS))
        daily_months = int(config.get("daily_retention_months", DEFAULT_DAILY_RETENTION_MONTHS))
        dropped, deleted = [], 0
        names = [row[0] for row in connection.execute(text(RUN_PARTITIONS_SQL))]
        connection.commit()
        for name in expired_run_partitions(names, today, run_days):
            # DROP takes ACCESS EXCLUSIVE on the parent: time out instead of blocking readers
            try:
                connection.execute(text(f"SET LOCAL lock_timeout = {DDL_LOCK_TIMEOUT_MS}"))
                connection.execute(text(f"DROP TABLE IF EXISTS {c.POSTGRES_SCHEMA}.{name}"))
                connection.commit()
                dropped.append(name)
            except:  # pylint: disable=W0702
                connection.rollback()
                log.warning("usage: could not drop %s now, retrying next night", name)
        before = _subtract_months(today, daily_months)
        while True:
            result = connection.execute(
                text(DAILY_RETENTION_SQL), {"before": before, "limit": DAILY_DELETE_BATCH},
            )
            connection.commit()
            deleted += result.rowcount or 0
            if (result.rowcount or 0) < DAILY_DELETE_BATCH:
                break
            _yield()
        if dropped or deleted:
            log.info(
                "usage: complexity retention dropped %s, deleted %s daily row(s)", dropped, deleted,
            )
        return {"dropped_partitions": dropped, "deleted_daily_rows": deleted}

    @web.method()
    def usage_complexity_nightly(self):
        """Nightly: calibration and retention. Scoring stays with the hourly job."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        #
        def job(connection):
            return {
                "calibrate": self.usage_complexity_calibrate_locked(connection),
                "retention": self.usage_complexity_retention(connection),
            }
        #
        return self.usage_complexity_locked(job)

    @web.method()
    def usage_complexity_backfill(self, days=30):
        """Re-score runs active in the last `days` days, a day at a time, then recalibrate."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        days = max(1, min(int(days), MAX_BACKFILL_DAYS))
        quiet = int(self.usage_complexity_config().get("quiet_minutes", DEFAULT_QUIET_MINUTES))
        #
        def job(connection):
            cutoff = _utcnow() - datetime.timedelta(minutes=quiet)
            state = self.usage_complexity_state(connection)
            scored, reached = self.usage_complexity_score_range(
                connection, cutoff - datetime.timedelta(days=days), cutoff,
            )
            # Only ever forward: a backfill must not make the hourly job redo or skip anything
            if state["scored_through"] is None or reached > state["scored_through"]:
                self.usage_complexity_set_state(
                    connection, scored_through=reached, last_scored_at=_utcnow(),
                )
            return {
                "scored": scored, "scored_through": reached.isoformat(),
                "calibrate": self.usage_complexity_calibrate_locked(connection),
            }
        #
        return self.usage_complexity_locked(job)

    @web.method()
    def usage_complexity_spawn(self, name, target):
        """Run a job in a daemon thread: the scheduler waits only seconds for an RPC reply."""
        def run():
            try:
                target()
            except:  # pylint: disable=W0702
                log.exception("usage: %s failed", name)
        _spawn(name, run)
        return {"started": name}

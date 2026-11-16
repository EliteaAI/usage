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

""" Complexity jobs: hourly run scoring, nightly calibration + retention, backfill (#7553) """

import datetime
import json
import threading
from collections import defaultdict

from sqlalchemy import func, select, text
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
DEFAULT_BATCH_SIZE = 1000
DEFAULT_MAX_BATCHES = 20
BACKFILL_MAX_BATCHES = 10000
DEFAULT_RUN_RETENTION_DAYS = 90
DEFAULT_DAILY_RETENTION_MONTHS = 13
# Older usage rows of a run are ignored; bounds the scan to recent usage_event partitions
RUN_LOOKBACK_DAYS = 35
CALIBRATION_WINDOW_DAYS = 30
CALIBRATION_RUNS_PER_ENTITY = 50
ENTITY_LABEL_MIN_RUNS = 3
DAILY_DELETE_BATCH = 5000

# Embedding calls carry a run_id from RAG tools but are not reasoning steps
_LLM_FILTER = (
    "event_type = 'llm' AND NOT is_error "
    "AND COALESCE(endpoint, '') !~ '/(embeddings|embed)/?$'"
)

CANDIDATES_SQL = f"""
SELECT project_id, run_id, max(ts) AS last_event_at
FROM {c.POSTGRES_SCHEMA}.usage_event
WHERE ts > :since AND run_id IS NOT NULL AND root_entity_id IS NOT NULL
  AND event_type IN ('llm', 'tool')
GROUP BY project_id, run_id
HAVING max(ts) <= :cutoff
   AND (max(ts), project_id, run_id) > (:after_ts, :after_project_id, :after_run_id)
ORDER BY max(ts), project_id, run_id
LIMIT :limit
"""

RUNS_SQL = f"""
SELECT e.project_id, e.run_id, min(e.ts) AS ts, max(e.ts) AS last_event_at,
       max(e.conversation_id) AS conversation_id, max(e.trigger_source) AS trigger_source,
       max(e.root_entity_type) AS root_entity_type, max(e.root_entity_id) AS root_entity_id,
       max(e.root_entity_version_id) AS root_entity_version_id,
       max(e.root_entity_project_id) AS root_entity_project_id,
       COALESCE(max(e.input_tokens) FILTER (WHERE {_LLM_FILTER}), 0) AS peak_input_tokens,
       count(*) FILTER (WHERE {_LLM_FILTER}) AS llm_calls,
       count(*) FILTER (WHERE e.event_type = 'tool') AS tool_calls
FROM {c.POSTGRES_SCHEMA}.usage_event e
JOIN unnest(CAST(:project_ids AS bigint[]), CAST(:run_ids AS text[])) AS k(project_id, run_id)
  ON e.project_id = k.project_id AND e.run_id = k.run_id
WHERE e.ts > :lookback AND e.root_entity_id IS NOT NULL AND e.event_type IN ('llm', 'tool')
GROUP BY e.project_id, e.run_id
"""

TOOLS_SQL = f"""
SELECT e.project_id, e.run_id, e.tool_name, e.meta ->> 'toolkit_type' AS toolkit_type,
       count(*) AS calls
FROM {c.POSTGRES_SCHEMA}.usage_event e
JOIN unnest(CAST(:project_ids AS bigint[]), CAST(:run_ids AS text[])) AS k(project_id, run_id)
  ON e.project_id = k.project_id AND e.run_id = k.run_id
WHERE e.ts > :lookback AND e.root_entity_id IS NOT NULL AND e.event_type = 'tool'
GROUP BY e.project_id, e.run_id, e.tool_name, e.meta ->> 'toolkit_type'
"""

DAILY_RUNS_SQL = f"""
SELECT r.project_id, r.root_entity_type, r.root_entity_id,
       (r.ts AT TIME ZONE 'UTC')::date AS day,
       r.peak_input_tokens, r.llm_calls, r.tool_calls,
       r.cu_context, r.cu_llm, r.cu_tools, r.cu_total
FROM {c.POSTGRES_SCHEMA}.{RUN_PARENT} r
JOIN unnest(
    CAST(:project_ids AS bigint[]), CAST(:types AS text[]),
    CAST(:entity_ids AS bigint[]), CAST(:days AS date[])
) AS k(project_id, root_entity_type, root_entity_id, day)
  ON r.project_id = k.project_id AND r.root_entity_type = k.root_entity_type
 AND r.root_entity_id = k.root_entity_id
 AND r.ts >= (k.day::timestamp AT TIME ZONE 'UTC')
 AND r.ts < ((k.day + 1)::timestamp AT TIME ZONE 'UTC')
"""

CALIBRATION_SQL = f"""
SELECT entity_project_id, root_entity_type, root_entity_id, cu_total FROM (
    SELECT COALESCE(root_entity_project_id, project_id) AS entity_project_id,
           root_entity_type, root_entity_id, cu_total,
           row_number() OVER (
               PARTITION BY COALESCE(root_entity_project_id, project_id),
                            root_entity_type, root_entity_id
               ORDER BY ts DESC
           ) AS rn
    FROM {c.POSTGRES_SCHEMA}.{RUN_PARENT}
    WHERE ts >= :since
) sample
WHERE rn <= :per_entity
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


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _months_between(start, end):
    return month_range((start.year, start.month), (end.year, end.month))


def _subtract_months(day, months):
    year, month = day.year, day.month - months
    while month <= 0:
        year, month = year - 1, month + 12
    return datetime.date(year, month, 1)


def ensure_run_partitions(connection, months):
    """Create missing monthly run partitions; False when the parent table is absent."""
    if not relation_exists(connection, c.POSTGRES_SCHEMA, RUN_PARENT, relkinds=("p",)):
        log.warning(
            "usage: %s.%s is missing; run the admin create_tables task",
            c.POSTGRES_SCHEMA, RUN_PARENT,
        )
        return False
    # Probe first: CREATE ... IF NOT EXISTS PARTITION OF still locks the parent
    for year, month in months:
        name = partition_name(year, month, RUN_PARENT)
        if not relation_exists(connection, c.POSTGRES_SCHEMA, name, relkinds=("r",)):
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
        key = (row["project_id"], row["run_id"])
        if not row["llm_calls"] and not row["tool_calls"]:
            continue
        counts = tools.get(key, [])
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
    """Distinct (project, entity type, entity id, UTC day) touched by a set of run rows."""
    keys = set()
    for row in run_rows:
        day = row["ts"].astimezone(datetime.timezone.utc).date()
        keys.add((row["project_id"], row["root_entity_type"], row["root_entity_id"], day))
    return sorted(keys)


def calibration_summary(sample_rows, config):
    """Thresholds plus run and entity percentiles from the nightly sample."""
    run_values = [float(row["cu_total"]) for row in sample_rows]
    by_entity = defaultdict(list)
    for row in sample_rows:
        by_entity[
            (row["entity_project_id"], row["root_entity_type"], row["root_entity_id"])
        ].append(float(row["cu_total"]))
    medians = [
        cx.quantile(sorted(values), 0.5)
        for values in by_entity.values() if len(values) >= ENTITY_LABEL_MIN_RUNS
    ]
    thresholds, source = cx.resolve_thresholds(run_values, len(by_entity), config)
    return {
        "thresholds": thresholds,
        "thresholds_source": source,
        "run_percentiles": cx.percentiles(run_values),
        "entity_percentiles": cx.percentiles(medians),
        "sample_runs": len(run_values),
        "sample_entities": len(by_entity),
    }


def _parse_days(param, default):
    """Admin task param: bare `N` or `days=N`."""
    value = str(param or "").strip()
    if value.startswith("days="):
        value = value[len("days="):]
    try:
        return max(1, min(int(value), 90)) if value else default
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
        """Run job() under the cross-replica advisory lock; None when another job holds it."""
        with db.engine.connect() as lock_connection:
            if not lock_connection.execute(
                    select(func.pg_try_advisory_lock(COMPLEXITY_ADVISORY_LOCK_KEY)),
            ).scalar():
                log.info("usage: complexity job skipped, another one is running")
                return None
            try:
                return job()
            finally:
                try:
                    lock_connection.execute(
                        select(func.pg_advisory_unlock(COMPLEXITY_ADVISORY_LOCK_KEY)),
                    )
                except:  # pylint: disable=W0702
                    log.exception("usage: failed to release the complexity lock")

    @web.method()
    def usage_complexity_state(self, connection):
        """The single state row as a dict, created empty on first use."""
        table = EntityComplexityState.__table__
        connection.execute(
            insert(table).values(id=STATE_ID).on_conflict_do_nothing(index_elements=["id"]),
        )
        row = connection.execute(select(table).where(table.c.id == STATE_ID)).mappings().first()
        return dict(row)

    @web.method()
    def usage_complexity_score_batch(self, connection, since, cutoff, limit, after=None):  # pylint: disable=R0913,R0917
        """Score up to limit quiet runs; returns (candidates seen, rows scored, keyset cursor)."""
        candidates = connection.execute(
            text(CANDIDATES_SQL), {
                "since": since, "cutoff": cutoff, "limit": limit,
                "after_ts": after[0] if after else since,
                "after_project_id": after[1] if after else -1,
                "after_run_id": after[2] if after else "",
            },
        ).mappings().all()
        if not candidates:
            return 0, 0, None
        #
        params = {
            "project_ids": [row["project_id"] for row in candidates],
            "run_ids": [row["run_id"] for row in candidates],
            "lookback": cutoff - datetime.timedelta(days=RUN_LOOKBACK_DAYS),
        }
        run_rows = connection.execute(text(RUNS_SQL), params).mappings().all()
        tool_rows = connection.execute(text(TOOLS_SQL), params).mappings().all()
        rows = build_run_rows(run_rows, tool_rows, cx.load_tool_weights(), _utcnow())
        #
        if rows:
            months = sorted({(row["ts"].year, row["ts"].month) for row in rows})
            if not ensure_run_partitions(connection, months):
                return 0, 0, None
            table = EntityComplexityRun.__table__
            statement = insert(table).values(rows)
            updatable = [k for k in rows[0] if k not in ("project_id", "run_id", "ts")]
            connection.execute(statement.on_conflict_do_update(
                index_elements=["project_id", "run_id", "ts"],
                set_={k: statement.excluded[k] for k in updatable},
            ))
            self.usage_complexity_rebuild_daily(connection, daily_keys(rows))
        connection.commit()
        #
        last = candidates[-1]
        return len(candidates), len(rows), (last["last_event_at"], last["project_id"], last["run_id"])

    @web.method()
    def usage_complexity_rebuild_daily(self, connection, keys):
        """Recompute daily rows for the given keys from run rows; idempotent."""
        if not keys:
            return 0
        runs = connection.execute(text(DAILY_RUNS_SQL), {
            "project_ids": [k[0] for k in keys], "types": [k[1] for k in keys],
            "entity_ids": [k[2] for k in keys], "days": [k[3] for k in keys],
        }).mappings().all()
        grouped = defaultdict(list)
        for row in runs:
            grouped[(row["project_id"], row["root_entity_type"], row["root_entity_id"], row["day"])] \
                .append(row)
        values = []
        for key, items in grouped.items():
            values.append({
                "project_id": key[0], "root_entity_type": key[1],
                "root_entity_id": key[2], "day": key[3], **cx.daily_rollup(items),
                "updated_at": _utcnow(),
            })
        if values:
            table = EntityComplexityDaily.__table__
            statement = insert(table).values(values)
            pk = ["project_id", "root_entity_type", "root_entity_id", "day"]
            connection.execute(statement.on_conflict_do_update(
                index_elements=pk,
                set_={k: statement.excluded[k] for k in values[0] if k not in pk},
            ))
        return len(values)

    @web.method()
    def usage_complexity_score(self, since=None, max_batches=None):
        """Score every run that went quiet since the watermark (or since `since`)."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        config = self.usage_complexity_config()
        quiet = int(config.get("quiet_minutes", DEFAULT_QUIET_MINUTES))
        batch_size = int(config.get("batch_size", DEFAULT_BATCH_SIZE))
        max_batches = int(max_batches or config.get("max_batches_per_run", DEFAULT_MAX_BATCHES))
        cutoff = _utcnow() - datetime.timedelta(minutes=quiet)
        #
        def job():
            scored = 0
            with db.engine.connect() as connection:
                state = self.usage_complexity_state(connection)
                connection.commit()
                start = since or state["scored_through"] \
                    or cutoff - datetime.timedelta(days=1)
                watermark, cursor = start, None
                for _ in range(max_batches):
                    seen, count, cursor = self.usage_complexity_score_batch(
                        connection, start, cutoff, batch_size, cursor,
                    )
                    scored += count
                    if cursor is None or seen < batch_size:
                        watermark = cutoff
                        break
                    # Capped mid-way: resume from the last fully scored quiet time next hour
                    watermark = cursor[0] - datetime.timedelta(microseconds=1)
                # A backfill never moves the watermark back
                if state["scored_through"] is not None and watermark < state["scored_through"]:
                    watermark = state["scored_through"]
                table = EntityComplexityState.__table__
                connection.execute(table.update().where(table.c.id == STATE_ID).values(
                    scored_through=watermark, last_scored_at=_utcnow(),
                ))
                connection.commit()
            log.info("usage: complexity scored %s run(s) since %s", scored, start)
            return {"scored": scored, "scored_through": watermark.isoformat()}
        #
        return self.usage_complexity_locked(job)

    @web.method()
    def usage_complexity_calibrate(self):
        """Nightly: thresholds, percentiles and computed_at from the last 30 days of runs."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        config = self.usage_complexity_config()
        #
        def job():
            with db.engine.connect() as connection:
                self.usage_complexity_state(connection)
                sample = connection.execute(text(CALIBRATION_SQL), {
                    "since": _utcnow() - datetime.timedelta(days=CALIBRATION_WINDOW_DAYS),
                    "per_entity": CALIBRATION_RUNS_PER_ENTITY,
                }).mappings().all()
                summary = calibration_summary(sample, config)
                table = EntityComplexityState.__table__
                connection.execute(table.update().where(table.c.id == STATE_ID).values(
                    computed_at=_utcnow(), **summary,
                ))
                connection.commit()
            log.info(
                "usage: complexity thresholds %s (%s) from %s run(s)",
                json.dumps(summary["thresholds"]), summary["thresholds_source"],
                summary["sample_runs"],
            )
            return summary
        #
        return self.usage_complexity_locked(job)

    @web.method()
    def usage_complexity_retention(self, today=None):
        """Drop run partitions past retention and batch-delete old daily rows."""
        config = self.usage_complexity_config()
        today = today or _utcnow().date()
        run_days = int(config.get("run_retention_days", DEFAULT_RUN_RETENTION_DAYS))
        daily_months = int(config.get("daily_retention_months", DEFAULT_DAILY_RETENTION_MONTHS))
        dropped, deleted = [], 0
        with db.engine.connect() as connection:
            names = [row[0] for row in connection.execute(text(RUN_PARTITIONS_SQL))]
            for name in expired_run_partitions(names, today, run_days):
                connection.execute(text(f"DROP TABLE IF EXISTS {c.POSTGRES_SCHEMA}.{name}"))
                connection.commit()
                dropped.append(name)
            before = _subtract_months(today, daily_months)
            while True:
                result = connection.execute(
                    text(DAILY_RETENTION_SQL), {"before": before, "limit": DAILY_DELETE_BATCH},
                )
                connection.commit()
                deleted += result.rowcount or 0
                if (result.rowcount or 0) < DAILY_DELETE_BATCH:
                    break
        if dropped or deleted:
            log.info("usage: complexity retention dropped %s, deleted %s daily row(s)", dropped, deleted)
        return {"dropped_partitions": dropped, "deleted_daily_rows": deleted}

    @web.method()
    def usage_complexity_nightly(self):
        """One nightly pass: catch-up scoring, calibration, retention."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        result = {"score": self.usage_complexity_score()}
        result["calibrate"] = self.usage_complexity_calibrate()
        result["retention"] = self.usage_complexity_locked(self.usage_complexity_retention)
        return result

    @web.method()
    def usage_complexity_backfill(self, days=30):
        """Score the last `days` days of runs, then recalibrate."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        since = _utcnow() - datetime.timedelta(days=days)
        with db.engine.connect() as connection:
            if not ensure_run_partitions(connection, _months_between(since, _utcnow())):
                return {"skipped": "missing table"}
        return {
            # An admin-run backfill should finish, not stop at the hourly batch cap
            "score": self.usage_complexity_score(since=since, max_batches=BACKFILL_MAX_BATCHES),
            "calibrate": self.usage_complexity_calibrate(),
        }

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

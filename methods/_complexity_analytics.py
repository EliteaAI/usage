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

""" Complexity analytics reads (#7554): daily rollup for entities, run table for one entity """

import datetime

from sqlalchemy import bindparam, text

from tools import config as c  # pylint: disable=E0401

from . import _complexity as cx

DAILY = f"{c.POSTGRES_SCHEMA}.entity_complexity_daily"
RUNS = f"{c.POSTGRES_SCHEMA}.entity_complexity_run"
STATE = f"{c.POSTGRES_SCHEMA}.entity_complexity_state"

ENTITY_SORTS = {
    "cu_median": "cu_median", "cu_avg": "a.cu_avg", "cu_max": "a.cu_max", "runs": "a.runs",
    "cu_context": "a.cu_context", "cu_llm": "a.cu_llm", "cu_tools": "a.cu_tools",
}
RUN_SORTS = {"ts": "ts", "cu_total": "cu_total"}

_HIST_SUMS = ", ".join(f"sum(cu_histogram[{i + 1}])" for i in range(cx.HISTOGRAM_BUCKETS))

# Interpolated median inside the bucket holding the middle run; mirrors cx.histogram_median.
# With one or two runs the average is the exact median; an inconsistent histogram keeps the row with a NULL median.
_ENTITY_SQL = f"""
WITH a AS (
    SELECT root_entity_project_id, root_entity_type, root_entity_id,
           sum(runs)::bigint AS runs,
           sum(cu_total_sum) / sum(runs) AS cu_avg,
           sum(cu_context_sum) / sum(runs) AS cu_context,
           sum(cu_llm_sum) / sum(runs) AS cu_llm,
           sum(cu_tools_sum) / sum(runs) AS cu_tools,
           max(cu_max) AS cu_max,
           ARRAY[{_HIST_SUMS}]::bigint[] AS hist
    FROM {DAILY}
    WHERE project_id = :project_id AND day >= :day_from AND day <= :day_to {{key_filter}}
    GROUP BY root_entity_project_id, root_entity_type, root_entity_id
    HAVING sum(runs) > 0
)
SELECT a.*, CASE WHEN a.runs <= 2 THEN a.cu_avg ELSE m.cu_median END AS cu_median,
       count(*) OVER () AS total
FROM a
LEFT JOIN LATERAL (
    SELECT lo + (greatest(lo, least(hi, a.cu_max)) - lo) * (a.runs / 2.0 - (cum - n)) / n AS cu_median
    FROM (
        SELECT n, cum,
               CASE WHEN k = 1 THEN 0 ELSE power(2, k - 2) END AS lo,
               CASE WHEN k = {cx.HISTOGRAM_BUCKETS} THEN a.cu_max ELSE power(2, k - 1) END AS hi
        FROM (
            SELECT n, k, sum(n) OVER (ORDER BY k) AS cum
            FROM unnest(a.hist) WITH ORDINALITY AS u(n, k)
        ) c
        WHERE n > 0 AND cum >= a.runs / 2.0
        ORDER BY k LIMIT 1
    ) b
) m ON true
ORDER BY {{order}}, a.root_entity_project_id, a.root_entity_type, a.root_entity_id
LIMIT :limit OFFSET :offset
"""

_KEY_FILTER = (
    "AND root_entity_project_id = :entity_project_id AND root_entity_type = :entity_type"
    " AND root_entity_id = :entity_id"
)

_DAILY_TREND_SQL = f"""
SELECT day, sum(runs)::bigint AS runs, sum(cu_total_sum) / sum(runs) AS cu_avg, max(cu_max) AS cu_max
FROM {DAILY}
WHERE project_id = :project_id AND day >= :day_from AND day <= :day_to {_KEY_FILTER}
GROUP BY day HAVING sum(runs) > 0 ORDER BY day
"""

_RUNS_SQL = f"""
SELECT run_id, conversation_id, ts, last_event_at, trigger_source, root_entity_version_id,
       peak_input_tokens, llm_calls, tool_calls, tool_counts,
       cu_context, cu_llm, cu_tools, cu_total, formula_version, tool_weights_version, scored_at,
       count(*) OVER () AS total
FROM {RUNS}
WHERE project_id = :project_id AND ts >= :dt_from AND ts < :dt_to {_KEY_FILTER} {{run_filter}}
ORDER BY {{order}}, ts DESC, run_id
LIMIT :limit OFFSET :offset
"""

# Exact median from the run table; one entity's runs in the window stay small enough for this.
_EXACT_STATS = """count(*)::bigint AS runs,
       avg(cu_total) AS cu_avg, max(cu_total) AS cu_max,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY cu_total) AS cu_median,
       avg(cu_context) AS cu_context, avg(cu_llm) AS cu_llm, avg(cu_tools) AS cu_tools,
       max(ts) AS last_run_at"""

_EXACT_WHERE = f"FROM {RUNS} WHERE project_id = :project_id AND ts >= :dt_from AND ts < :dt_to {_KEY_FILTER}"

_VERSIONS_SQL = f"""
SELECT root_entity_version_id, {_EXACT_STATS}
{_EXACT_WHERE}
GROUP BY root_entity_version_id
ORDER BY last_run_at DESC
"""

_EXACT_SUMMARY_SQL = f"SELECT {_EXACT_STATS} {_EXACT_WHERE}"


def exact_summary(rollup, exact):
    """Rollup row with exact run-table stats, unless part of the range is past run retention."""
    if not exact or int(exact["runs"] or 0) != int(rollup["runs"] or 0):
        return rollup
    columns = ("cu_median", "cu_avg", "cu_max", "cu_context", "cu_llm", "cu_tools")
    return {**rollup, **{column: exact[column] for column in columns}}


def day_bounds(dt_from, dt_to):
    """Inclusive UTC days covering the [dt_from, dt_to) window."""
    return dt_from.date(), (dt_to - datetime.timedelta(microseconds=1)).date()


def order_clause(sorts, sort_by, sort_order, default):
    """Whitelisted column plus direction; unknown input falls back to the default."""
    column = sorts.get(sort_by) or sorts[default]
    return f"{column} {'ASC' if sort_order == 'asc' else 'DESC'} NULLS LAST"


def entity_statement(order, key=False):
    """List (or one-key) aggregation over the daily rollup."""
    return text(_ENTITY_SQL.format(key_filter=_KEY_FILTER if key else "", order=order))


def daily_trend_statement():
    """Per-day runs and average CU for one entity."""
    return text(_DAILY_TREND_SQL)


# One row per conversation: its most complex run, so N ids never return more than N rows
_CONVERSATION_RUNS_SQL = f"""
SELECT *, count(*) OVER () AS total FROM (
    SELECT DISTINCT ON (conversation_id)
           run_id, conversation_id, ts, last_event_at, trigger_source, root_entity_version_id,
           peak_input_tokens, llm_calls, tool_calls, tool_counts,
           cu_context, cu_llm, cu_tools, cu_total, formula_version, tool_weights_version, scored_at
    FROM {RUNS}
    WHERE project_id = :project_id AND ts >= :dt_from AND ts < :dt_to {_KEY_FILTER} {{run_filter}}
    ORDER BY conversation_id, cu_total DESC, ts DESC
) top
ORDER BY {{order}}, ts DESC, run_id
LIMIT :limit OFFSET :offset
"""


def runs_statement(order, version_id=None, conversation_ids=None):
    """Scored runs plus params; with conversation ids, the top run of each (an empty list matches nothing)."""
    clauses, params = [], {}
    if version_id is not None:
        clauses.append("AND root_entity_version_id = :version_id")
        params["version_id"] = version_id
    if conversation_ids is not None:
        clauses.append("AND conversation_id IN :conversation_ids" if conversation_ids else "AND false")
        if conversation_ids:
            params["conversation_ids"] = list(conversation_ids)
    sql = _CONVERSATION_RUNS_SQL if conversation_ids is not None else _RUNS_SQL
    statement = text(sql.format(order=order, run_filter=" ".join(clauses)))
    if "conversation_ids" in params:
        statement = statement.bindparams(bindparam("conversation_ids", expanding=True))
    return statement, params


def exact_summary_statement():
    """All-version exact stats of one entity from the run table."""
    return text(_EXACT_SUMMARY_SQL)


def versions_statement():
    """Per-version summary of one entity's scored runs."""
    return text(_VERSIONS_SQL)


def state_statement():
    """The single calibration/watermark row."""
    return text(f"SELECT * FROM {STATE} WHERE id = 1")


def _num(value, digits=2):
    return None if value is None else round(float(value), digits)


def _iso(value):
    return value.isoformat() if value is not None else None


def thresholds_of(state):
    """Stored thresholds, or the defaults before the first calibration."""
    return cx.normalize_thresholds((state or {}).get("thresholds"))


def meta(state, enabled, now=None):
    """Freshness block shared by both endpoints."""
    state = state or {}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {
        "enabled": bool(enabled),
        "formula_version": cx.FORMULA_VERSION,
        "thresholds": thresholds_of(state),
        "thresholds_source": state.get("thresholds_source") or "default",
        "thresholds_computed_at": _iso(state.get("computed_at")),
        "as_of": _iso(state.get("last_scored_at")),
        "scored_through": _iso(state.get("scored_through")),
        "stale": cx.is_stale(state.get("computed_at"), now),
    }


def _summary(row, state):
    runs = int(row["runs"] or 0)
    median = _num(row["cu_median"])
    pending = runs < cx.ENTITY_LABEL_MIN_RUNS or median is None
    return {
        "runs": runs,
        "cu_median": median,
        "cu_avg": _num(row["cu_avg"]),
        "cu_max": _num(row["cu_max"]),
        "breakdown": {
            "context": _num(row["cu_context"]),
            "reasoning": _num(row["cu_llm"]),
            "tools": _num(row["cu_tools"]),
        },
        "label": None if pending else cx.label_for(median, thresholds_of(state)),
        "label_pending": pending,
        "percentile": None if pending else cx.percentile_of(median, (state or {}).get("entity_percentiles")),
    }


def entity_row(row, state):
    """API shape of one aggregated entity; labelled by median once it has enough runs."""
    return {
        "entity_id": row["root_entity_id"],
        "entity_project_id": row["root_entity_project_id"],
        "entity_type": row["root_entity_type"],
        **_summary(row, state),
    }


def version_row(row, state):
    """API shape of one version's summary; same labelling rule as the entity."""
    return {
        "version_id": row["root_entity_version_id"],
        **_summary(row, state),
        "last_run_at": _iso(row["last_run_at"]),
    }


def run_row(row, state):
    """API shape of one scored run."""
    cu_total = float(row["cu_total"] or 0)
    return {
        "run_id": row["run_id"],
        "conversation_id": row["conversation_id"],
        "started_at": _iso(row["ts"]),
        "last_event_at": _iso(row["last_event_at"]),
        "trigger_source": row["trigger_source"],
        "version_id": row["root_entity_version_id"],
        "peak_input_tokens": int(row["peak_input_tokens"] or 0),
        "llm_calls": int(row["llm_calls"] or 0),
        "tool_calls": int(row["tool_calls"] or 0),
        "tool_counts": row["tool_counts"] or {},
        "cu_total": _num(cu_total),
        "breakdown": {
            "context": _num(row["cu_context"]),
            "reasoning": _num(row["cu_llm"]),
            "tools": _num(row["cu_tools"]),
        },
        "label": cx.label_for(cu_total, thresholds_of(state)),
        "percentile": cx.percentile_of(cu_total, (state or {}).get("run_percentiles")),
        "formula_version": row["formula_version"],
        "tool_weights_version": row["tool_weights_version"],
        "scored_at": _iso(row["scored_at"]),
    }

"""#7554 — Complexity analytics API: histogram median, percentiles, freshness, row shapes, gate.

The SQL median and cx.histogram_median must agree; the live check (API vs percentile_cont on
project 3) found the bucket interpolation within ~1.3 CU of the exact median.
"""
import datetime
import statistics
import types

import pytest

from fixtures.usage_event_table import load_api_module
from usage.methods import _complexity as cx
from usage.methods import _complexity_analytics as ca

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 16, 12, tzinfo=UTC)


def hist(**buckets):
    counts = [0] * cx.HISTOGRAM_BUCKETS
    for key, value in buckets.items():
        counts[int(key[1:])] = value
    return counts


class TestHistogramMedian:
    def test_empty_histogram_has_no_median(self):
        assert cx.histogram_median([0] * cx.HISTOGRAM_BUCKETS, 0) is None
        assert cx.histogram_median(None, 0) is None

    def test_one_or_two_runs_use_the_exact_average(self):
        assert cx.histogram_median(hist(b3=1), 5.0, cu_avg=5.0) == 5.0
        assert cx.histogram_median(hist(b2=1, b4=1), 9.0, cu_avg=6.0) == 6.0

    def test_interpolates_inside_the_middle_bucket(self):
        # 4 runs in [4, 8): the middle (2 of 4) is half way through the bucket
        assert cx.histogram_median(hist(b3=4), 8.0) == pytest.approx(6.0)

    def test_the_bucket_is_capped_by_the_largest_seen_cu(self):
        # [8, 16) but nothing above 10 was ever scored: interpolate inside [8, 10]
        assert cx.histogram_median(hist(b4=4), 10.0) == pytest.approx(9.0)

    def test_the_open_last_bucket_ends_at_cu_max(self):
        last = cx.HISTOGRAM_BUCKETS - 1
        counts = [0] * cx.HISTOGRAM_BUCKETS
        counts[last] = 4
        lo = 2 ** (last - 1)
        assert cx.histogram_median(counts, lo + 1000) == pytest.approx(lo + 500)

    def test_skips_to_the_bucket_holding_the_middle_run(self):
        assert 1 <= cx.histogram_median(hist(b0=1, b1=3, b5=1), 20.0) < 2


class TestPercentileAndStaleness:
    def test_percentile_is_the_share_of_breakpoints_below(self):
        breakpoints = [float(i) for i in range(1, 100)]
        assert cx.percentile_of(0.5, breakpoints) == 0
        assert cx.percentile_of(50.0, breakpoints) == 50
        assert cx.percentile_of(1000.0, breakpoints) == 99

    def test_no_breakpoints_or_value_means_no_percentile(self):
        assert cx.percentile_of(5.0, None) is None
        assert cx.percentile_of(None, [1.0]) is None

    def test_stale_without_calibration_or_after_26h(self):
        assert cx.is_stale(None, NOW)
        assert not cx.is_stale(NOW - datetime.timedelta(hours=25), NOW)
        assert cx.is_stale(NOW - datetime.timedelta(hours=27), NOW)


STATE = {
    "thresholds": {"medium": 10.0, "high": 30.0, "extreme": 100.0},
    "thresholds_source": "calibrated",
    "entity_percentiles": [float(i) for i in range(1, 100)],
    "run_percentiles": [float(i) for i in range(1, 100)],
    "computed_at": NOW - datetime.timedelta(hours=1),
    "last_scored_at": NOW, "scored_through": NOW,
}


def entity(runs, median, entity_project_id=3):
    return {
        "root_entity_id": 7, "root_entity_project_id": entity_project_id, "root_entity_type": "application",
        "runs": runs, "cu_median": median, "cu_avg": median, "cu_max": median,
        "cu_context": 1.0, "cu_llm": 2.0, "cu_tools": 3.0,
    }


class TestShapes:
    def test_entity_label_is_pending_below_three_runs(self):
        row = ca.entity_row(entity(2, 40.0), STATE)
        assert row["label"] is None and row["label_pending"] and row["percentile"] is None

    def test_entity_is_labelled_by_its_median(self):
        row = ca.entity_row(entity(5, 40.0), STATE)
        assert (row["label"], row["percentile"]) == ("H", 40)
        assert row["breakdown"] == {"context": 1.0, "reasoning": 2.0, "tools": 3.0}

    def test_public_agent_keeps_its_owning_project(self):
        assert ca.entity_row(entity(5, 1.0, entity_project_id=1), STATE)["entity_project_id"] == 1

    def test_run_label_uses_the_run_total(self):
        run = {
            "run_id": "r", "conversation_id": None, "ts": NOW, "last_event_at": NOW, "trigger_source": None,
            "root_entity_version_id": None, "peak_input_tokens": 1, "llm_calls": 1, "tool_calls": 0,
            "tool_counts": None, "cu_context": 0, "cu_llm": 1, "cu_tools": 0, "cu_total": 120.0,
            "formula_version": 1, "tool_weights_version": 1, "scored_at": NOW,
        }
        row = ca.run_row(run, STATE)
        assert (row["label"], row["tool_counts"]) == ("X", {})

    def test_version_row_uses_the_entity_labelling_rule(self):
        # A version with fewer than three runs stays pending, like an entity.
        row = {**entity(5, 40.0), "root_entity_version_id": 12, "last_run_at": NOW}
        assert ca.version_row(row, STATE)["version_id"] == 12 and ca.version_row(row, STATE)["label"] == "H"
        assert ca.version_row({**row, "runs": 2}, STATE)["label_pending"]

    def test_summary_uses_the_exact_median_like_the_version_row(self):
        # #7556 review: 8 runs straddling the 8-CU bucket edge; the histogram gives 8.0, exact is 9.07.
        values = [6.193, 6.159, 14.907, 11.952, 4.476, 12.854, 5.037, 17.588]
        exact_median = statistics.median(values)
        rollup = {**entity(8, 8.0), "cu_avg": 9.9, "cu_max": 17.588}
        exact = {"runs": 8, "cu_median": exact_median, "cu_avg": sum(values) / 8, "cu_max": max(values),
                 "cu_context": 1.0, "cu_llm": 2.0, "cu_tools": 3.0}
        summary = ca.entity_row(ca.exact_summary(rollup, exact), STATE)
        version = ca.version_row({**exact, "root_entity_version_id": 30185, "last_run_at": NOW}, STATE)
        assert summary["cu_median"] == version["cu_median"] == 9.07
        assert summary["percentile"] == version["percentile"] and summary["label"] == version["label"]

    def test_summary_keeps_the_rollup_past_run_retention(self):
        rollup = entity(8, 8.0)
        assert ca.exact_summary(rollup, {"runs": 5, "cu_median": 9.0}) is rollup
        assert ca.exact_summary(rollup, None) is rollup

    def test_summary_stays_exact_when_the_run_table_holds_more(self):
        # #1570 review: a mid-day timestamp bound can count more exact runs than whole rollup days
        exact = {"runs": 9, "cu_median": 9.07, "cu_avg": 9.9, "cu_max": 17.6,
                 "cu_context": 1.0, "cu_llm": 2.0, "cu_tools": 3.0}
        assert ca.exact_summary(entity(8, 8.0), exact)["cu_median"] == 9.07

    def test_meta_reports_freshness_and_defaults_before_calibration(self):
        assert ca.meta(STATE, True, NOW)["stale"] is False
        empty = ca.meta(None, False, NOW)
        assert empty["thresholds"] == cx.DEFAULT_THRESHOLDS and empty["stale"] and not empty["enabled"]


class TestSql:
    def test_unknown_sort_falls_back_to_the_default(self):
        assert ca.order_clause(ca.ENTITY_SORTS, "drop table", "desc", "cu_median") == "cu_median DESC NULLS LAST"
        assert ca.order_clause(ca.RUN_SORTS, "cu_total", "asc", "ts") == "cu_total ASC NULLS LAST"

    def test_entity_statement_reads_only_the_daily_rollup(self):
        sql = str(ca.entity_statement("runs DESC"))
        assert "entity_complexity_daily" in sql and "entity_complexity_run" not in sql
        assert "root_entity_project_id = :entity_project_id" in str(ca.entity_statement("runs DESC", key=True))

    def test_the_median_join_keeps_entities_with_an_inconsistent_histogram(self):
        assert "LEFT JOIN LATERAL" in str(ca.entity_statement("runs DESC")) and ") m ON true" in str(ca.entity_statement("runs DESC"))

    def test_runs_filter_by_version_only_when_asked(self):
        assert ca.runs_statement("ts DESC")[1] == {}
        statement, params = ca.runs_statement("ts DESC", version_id=12)
        assert "root_entity_version_id = :version_id" in str(statement) and params == {"version_id": 12}

    def test_conversation_filter_uses_the_raw_column_and_empty_matches_nothing(self):
        statement, params = ca.runs_statement("ts DESC", conversation_ids=["a"])
        assert "conversation_id IN" in str(statement) and "::text" not in str(statement)
        # One row per conversation, its most complex run, so the result never exceeds the ids sent
        assert "DISTINCT ON (conversation_id)" in str(statement) and "cu_total DESC" in str(statement)
        assert "DISTINCT ON" not in str(ca.runs_statement("ts DESC")[0])
        assert params == {"conversation_ids": ["a"]}
        empty, params = ca.runs_statement("ts DESC", conversation_ids=[])
        assert "AND false" in str(empty) and params == {}

    def test_versions_statement_groups_the_run_table_by_version(self):
        sql = str(ca.versions_statement())
        assert "entity_complexity_run" in sql and "GROUP BY root_entity_version_id" in sql

    def test_day_bounds_treat_date_to_as_exclusive(self):
        start = datetime.datetime(2026, 11, 9, tzinfo=UTC)
        assert ca.day_bounds(start, datetime.datetime(2026, 11, 16, tzinfo=UTC)) == (
            datetime.date(2026, 11, 9), datetime.date(2026, 11, 15),
        )


class TestEndpointGate:
    @pytest.mark.parametrize("name", ["analytics_complexity", "analytics_complexity_detail"])
    def test_disabled_returns_empty_rows_without_querying(self, monkeypatch, name):
        module = load_api_module(monkeypatch, name)
        module.request.args = {"entity_id": "7"}
        queries = []
        monkeypatch.setattr(module.an, "fetch_one", lambda statement: STATE)
        monkeypatch.setattr(module.an, "fetch_all", lambda statement: queries.append(statement) or [])
        handler = module.PromptLibAPI()
        handler.module = types.SimpleNamespace(usage_complexity_is_enabled=lambda: False)
        body, status = handler.get(3)
        assert status == 200 and body["meta"]["enabled"] is False and body["total"] == 0
        assert not queries

    @pytest.mark.parametrize("args", [
        {"entity_id": "7", "version_id": "abc"},
        {"entity_id": "7", "conversation_ids": ",".join(str(i) for i in range(101))},
    ])
    def test_detail_rejects_bad_run_filters(self, monkeypatch, args):
        module = load_api_module(monkeypatch, "analytics_complexity_detail")
        module.request.args = args
        assert module.PromptLibAPI().get(3)[1] == 400

    def test_conversation_lookup_runs_only_the_runs_query(self, monkeypatch):
        # #1570 review: Run History reads only runs, so summary, daily and versions are skipped
        module = load_api_module(monkeypatch, "analytics_complexity_detail")
        module.request.args = {"entity_id": "7", "conversation_ids": "a,b"}
        queries = []
        monkeypatch.setattr(module.an, "fetch_one", lambda statement: STATE)
        monkeypatch.setattr(module.an, "fetch_all", lambda statement: queries.append(str(statement)) or [])
        handler = module.PromptLibAPI()
        handler.module = types.SimpleNamespace(usage_complexity_is_enabled=lambda: True)
        body, status = handler.get(3)
        assert status == 200 and len(queries) == 1 and "DISTINCT ON (conversation_id)" in queries[0]
        assert body["summary"] is None and body["daily"] == [] and body["versions"] == []

    def test_detail_requires_an_entity(self, monkeypatch):
        module = load_api_module(monkeypatch, "analytics_complexity_detail")
        module.request.args = {"entity_type": "skill", "entity_id": "7"}
        assert module.PromptLibAPI().get(3)[1] == 400

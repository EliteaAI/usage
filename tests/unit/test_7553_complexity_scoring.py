"""#7553 — Complexity Units: formula v1, weight lookup, labels, thresholds, job helpers.

These pin the #7552 decisions: context capped at 30 CU, one CU per non-error LLM call,
tool calls weighted by the shipped v1 map, thresholds L<10 / M<30 / H<100 / X.
They also pin the review fixes: no time lookback, owning project in every key, SQL percentiles.
"""
import datetime
import json

import pytest

from usage.methods import _complexity as cx
from usage.methods import complexity as jobs

UTC = datetime.timezone.utc
WEIGHTS = {
    "version": 7, "default": 1.0,
    "weights": {"pyodide_sandbox": 3.0, "ask_user": 0.0, "github:read_file": 2.0, "index_data": 5.0},
}


class TestShippedWeights:
    def test_the_shipped_map_is_version_1_with_default_1(self):
        weights = cx.load_tool_weights()
        assert weights["version"] == 1 and weights["default"] == 1.0

    def test_it_matches_the_prototype_map(self, plugin_root):
        # The #7552 prototype map is the calibration source; drifting silently changes every score
        shipped = json.loads((plugin_root / "data" / "tool_weights_v1.json").read_text())
        assert shipped["weights"]["pyodide_sandbox"] == 3
        assert shipped["weights"]["index_data"] == 5
        assert shipped["weights"]["ask_user"] == 0


class TestToolWeight:
    def test_toolkit_qualified_key_wins(self):
        assert cx.tool_weight(WEIGHTS, "read_file", "github") == 2.0

    def test_falls_back_to_the_bare_name_then_default(self):
        assert cx.tool_weight(WEIGHTS, "read_file", "sharepoint") == 1.0
        assert cx.tool_weight(WEIGHTS, "pyodide_sandbox", "sandbox") == 3.0

    def test_rows_without_a_toolkit_type_use_the_bare_name(self):
        assert cx.tool_weight(WEIGHTS, "pyodide_sandbox", None) == 3.0

    def test_toolkit_instance_prefix_is_stripped(self):
        assert cx.bare_tool_name("gitlab-project-397__read_file") == "read_file"
        assert cx.tool_weight(WEIGHTS, "ImageGen___index_data") == 5.0

    def test_zero_weight_tools_count_nothing(self):
        assert cx.tool_weight(WEIGHTS, "ask_user") == 0.0


class TestScoreRun:
    def test_formula_v1(self):
        score = cx.score_run(12_500, 4, [("pyodide_sandbox", None, 2), ("read_file", None, 1)], WEIGHTS)
        assert score == {"cu_context": 12.5, "cu_llm": 4.0, "cu_tools": 7.0, "cu_total": 23.5}

    def test_context_is_capped_at_30(self):
        assert cx.score_run(200_000, 0, [], WEIGHTS)["cu_context"] == 30.0

    def test_tool_only_run_scores_its_tools(self):
        assert cx.score_run(0, 0, [("read_file", None, 3)], WEIGHTS)["cu_total"] == 3.0


class TestLabels:
    @pytest.mark.parametrize("cu,label", [(0, "L"), (9.99, "L"), (10, "M"), (30, "H"), (100, "X")])
    def test_default_labels(self, cu, label):
        assert cx.label_for(cu, cx.DEFAULT_THRESHOLDS) == label


class TestThresholds:
    CONFIG = {"calibration": {"min_runs": 200, "min_entities": 10}}
    QUANTILES = [12.0, 35.0, 110.0]

    def test_defaults_until_enough_runs(self):
        assert cx.resolve_thresholds(self.QUANTILES, 199, 50, self.CONFIG) == (
            cx.DEFAULT_THRESHOLDS, "default",
        )

    def test_defaults_until_enough_entities(self):
        assert cx.resolve_thresholds(self.QUANTILES, 500, 9, self.CONFIG)[1] == "default"

    def test_calibrated_quantiles_are_used_once_the_sample_is_big_enough(self):
        assert cx.resolve_thresholds(self.QUANTILES, 500, 20, self.CONFIG) == (
            {"medium": 12.0, "high": 35.0, "extreme": 110.0}, "calibrated",
        )

    def test_a_degenerate_sample_keeps_the_defaults(self):
        # All runs equal -> p75 == p90; non-increasing cut-offs would make a label unreachable
        assert cx.resolve_thresholds([4.0, 4.0, 9.0], 300, 20, self.CONFIG)[1] == "default"

    def test_no_sample_at_all(self):
        assert cx.resolve_thresholds(None, 0, 0, self.CONFIG)[1] == "default"

    def test_configured_defaults_are_used(self):
        config = {**self.CONFIG, "default_thresholds": {"medium": 5, "high": 20, "extreme": 50}}
        assert cx.resolve_thresholds(None, 0, 0, config)[0] == {
            "medium": 5.0, "high": 20.0, "extreme": 50.0,
        }

    def test_pins_override_single_values(self):
        config = {**self.CONFIG, "pinned_thresholds": {"medium": None, "high": 40, "extreme": None}}
        assert cx.resolve_thresholds(None, 0, 0, config) == (
            {"medium": 10.0, "high": 40.0, "extreme": 100.0}, "pinned",
        )

    def test_an_inconsistent_pin_is_ignored(self):
        config = {**self.CONFIG, "pinned_thresholds": {"high": 5}}
        assert cx.resolve_thresholds(None, 0, 0, config) == (cx.DEFAULT_THRESHOLDS, "default")

    def test_invalid_defaults_fall_back_to_the_built_in_ones(self):
        config = {"default_thresholds": {"medium": 50, "high": 20, "extreme": 10}}
        assert cx.resolve_thresholds(None, 0, 0, config)[0] == cx.DEFAULT_THRESHOLDS


def _run(project_id=3, run_id="r1", llm=2, tools=1, peak=4000, ts=None):
    ts = ts or datetime.datetime(2026, 11, 10, 23, 50, tzinfo=UTC)
    return {
        "project_id": project_id, "run_id": run_id, "ts": ts,
        "last_event_at": ts + datetime.timedelta(minutes=20),
        "conversation_id": "c1", "trigger_source": None,
        "root_entity_type": "application", "root_entity_id": 7,
        "root_entity_version_id": 70, "root_entity_project_id": 3,
        "peak_input_tokens": peak, "llm_calls": llm, "tool_calls": tools,
    }


class TestBuildRunRows:
    def test_joins_tool_counts_and_stamps_versions(self):
        tools = [
            {"project_id": 3, "run_id": "r1", "tool_name": "read_file", "toolkit_type": "github", "calls": 2},
            {"project_id": 3, "run_id": "r1", "tool_name": "ask_user", "toolkit_type": None, "calls": 1},
        ]
        (row,) = jobs.build_run_rows([_run(tools=3)], tools, WEIGHTS, "now")
        assert row["cu_tools"] == 4.0 and row["cu_total"] == 10.0
        assert row["tool_counts"] == {"github:read_file": 2, "ask_user": 1}
        assert row["formula_version"] == cx.FORMULA_VERSION and row["tool_weights_version"] == 7

    def test_a_run_with_no_work_is_not_scored(self):
        # Only errored LLM calls: nothing was metered as work, so there is nothing to label
        assert jobs.build_run_rows([_run(llm=0, tools=0, peak=0)], [], WEIGHTS, "now") == []


class TestDailyKeys:
    def test_day_is_the_utc_day_of_the_first_event(self):
        local = datetime.timezone(datetime.timedelta(hours=3))
        row = _run(ts=datetime.datetime(2026, 11, 11, 1, 0, tzinfo=local))
        assert jobs.daily_keys([row, row]) == [(3, 3, "application", 7, datetime.date(2026, 11, 10))]

    def test_a_public_agent_and_a_local_one_with_the_same_id_stay_apart(self):
        # Agent 7 owned by project 9 (public) and agent 7 of project 3 itself, both run from 3
        local, public = _run(), {**_run(run_id="r2"), "root_entity_project_id": 9}
        assert len(jobs.daily_keys([local, public])) == 2


class TestWindows:
    def test_covers_the_range_in_day_steps(self):
        start = datetime.datetime(2026, 11, 1, 12, tzinfo=UTC)
        end = datetime.datetime(2026, 11, 3, 18, tzinfo=UTC)
        result = jobs.windows(start, end)
        assert result[0] == (start, start + jobs.WINDOW)
        assert result[-1][1] == end and len(result) == 3

    def test_empty_when_nothing_to_do(self):
        now = datetime.datetime(2026, 11, 1, tzinfo=UTC)
        assert jobs.windows(now, now) == []


class TestCalibrationSummary:
    def test_reads_the_one_row_sql_result(self):
        row = {
            "sample_runs": 250, "sample_entities": 12, "calibration": [11.0, 33.0, 120.0],
            "run_percentiles": [1.23456] * 99, "entity_percentiles": None,
        }
        summary = jobs.calibration_summary(row, {})
        assert summary["thresholds_source"] == "calibrated"
        assert summary["run_percentiles"] == [1.235] * 99
        assert summary["entity_percentiles"] is None


class TestRetention:
    def test_only_whole_months_past_the_horizon_are_dropped(self):
        names = [
            "entity_complexity_run_202607", "entity_complexity_run_202608",
            "entity_complexity_run_202609", "entity_complexity_run_202611", "something_else",
        ]
        # 2026-11-16 minus 90 days = 2026-08-18: August still holds rows inside retention
        assert jobs.expired_run_partitions(names, datetime.date(2026, 11, 16), 90) == [
            "entity_complexity_run_202607",
        ]

    def test_subtract_months_crosses_the_year(self):
        assert jobs._subtract_months(datetime.date(2026, 11, 16), 13) == datetime.date(2025, 10, 1)


class TestParseDays:
    @pytest.mark.parametrize("param,days", [
        (None, 30), ("", 30), ("7", 7), ("days=14", 14), ("x", 30), ("500", 90), ("0", 1),
    ])
    def test_param(self, param, days):
        assert jobs._parse_days(param, 30) == days


class TestPostgresOnlyStatements:
    """The heavy work lives in SQL on purpose (single-CPU gevent pylon); pin that it stays there."""

    def test_percentiles_are_computed_by_postgres(self):
        assert jobs.CALIBRATION_SQL.count("percentile_cont") == 4

    def test_the_run_aggregate_has_no_time_lookback(self):
        # A lookback shifted min(ts) for runs straddling it and duplicated their row
        assert ":lookback" not in jobs.RUNS_SQL and ":lookback" not in jobs.TOOLS_SQL

    def test_window_candidates_are_bounded_on_both_sides(self):
        assert "e.ts > :start AND e.ts <= :end" in jobs.WINDOW_RUNS_SQL

    def test_daily_key_includes_the_owning_project(self):
        assert "ON CONFLICT (project_id, root_entity_project_id" in jobs.DAILY_UPSERT_SQL

    def test_insert_chunks_stay_far_below_the_bind_limit(self):
        assert jobs.CHUNK_SIZE * 30 < 32767

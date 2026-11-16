"""#7553 — Complexity Units: formula v1, weight lookup, labels, rollup, calibration.

These pin the #7552 decisions: context capped at 30 CU, one CU per non-error LLM call,
tool calls weighted by the shipped v1 map, thresholds L<10 / M<30 / H<100 / X.
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


class TestLabelsAndHistogram:
    @pytest.mark.parametrize("cu,label", [(0, "L"), (9.99, "L"), (10, "M"), (30, "H"), (100, "X")])
    def test_default_labels(self, cu, label):
        assert cx.label_for(cu, cx.DEFAULT_THRESHOLDS) == label

    @pytest.mark.parametrize("cu,bucket", [(0, 0), (0.9, 0), (1, 1), (3.9, 2), (4, 3), (10**9, 12)])
    def test_log2_buckets(self, cu, bucket):
        assert cx.histogram_bucket(cu) == bucket


class TestDailyRollup:
    def test_sums_max_and_histogram(self):
        runs = [
            {"peak_input_tokens": 1000, "llm_calls": 1, "tool_calls": 0,
             "cu_context": 1, "cu_llm": 1, "cu_tools": 0, "cu_total": 2},
            {"peak_input_tokens": 5000, "llm_calls": 3, "tool_calls": 2,
             "cu_context": 5, "cu_llm": 3, "cu_tools": 6, "cu_total": 14},
        ]
        row = cx.daily_rollup(runs)
        assert row["runs"] == 2 and row["cu_total_sum"] == 16 and row["cu_max"] == 14
        assert row["peak_input_tokens_sum"] == 6000 and row["tool_calls"] == 2
        assert sum(row["cu_histogram"]) == 2 and len(row["cu_histogram"]) == cx.HISTOGRAM_BUCKETS


class TestThresholds:
    CONFIG = {"calibration": {"min_runs": 200, "min_entities": 10}}

    def test_defaults_until_enough_runs(self):
        assert cx.resolve_thresholds([5.0] * 199, 50, self.CONFIG) == (cx.DEFAULT_THRESHOLDS, "default")

    def test_defaults_until_enough_entities(self):
        assert cx.resolve_thresholds([5.0] * 500, 9, self.CONFIG)[1] == "default"

    def test_calibrates_on_p75_p90_p99(self):
        sample = [float(v) for v in range(1, 1001)]
        thresholds, source = cx.resolve_thresholds(sample, 20, self.CONFIG)
        assert source == "calibrated"
        assert thresholds == {"medium": 750.2, "high": 900.1, "extreme": 990.0}

    def test_a_degenerate_sample_keeps_the_defaults(self):
        # All runs equal -> p75 == p90; non-increasing cut-offs would make a label unreachable
        assert cx.resolve_thresholds([4.0] * 300, 20, self.CONFIG)[1] == "default"

    def test_configured_defaults_are_used(self):
        config = {**self.CONFIG, "default_thresholds": {"medium": 5, "high": 20, "extreme": 50}}
        assert cx.resolve_thresholds([], 0, config)[0] == {"medium": 5.0, "high": 20.0, "extreme": 50.0}

    def test_pins_override_single_values(self):
        config = {**self.CONFIG, "pinned_thresholds": {"medium": None, "high": 40, "extreme": None}}
        assert cx.resolve_thresholds([], 0, config) == (
            {"medium": 10.0, "high": 40.0, "extreme": 100.0}, "pinned",
        )

    def test_an_inconsistent_pin_is_ignored(self):
        config = {**self.CONFIG, "pinned_thresholds": {"high": 5}}
        assert cx.resolve_thresholds([], 0, config) == (cx.DEFAULT_THRESHOLDS, "default")

    def test_invalid_defaults_fall_back_to_the_built_in_ones(self):
        config = {"default_thresholds": {"medium": 50, "high": 20, "extreme": 10}}
        assert cx.resolve_thresholds([], 0, config)[0] == cx.DEFAULT_THRESHOLDS


class TestPercentiles:
    def test_ninety_nine_breakpoints(self):
        result = cx.percentiles(range(1, 101))
        assert len(result) == 99 and result[49] == 50.5

    def test_empty_sample(self):
        assert cx.percentiles([]) is None


def _run(project_id=3, run_id="r1", llm=2, tools=1, peak=4000, ts=None):
    ts = ts or datetime.datetime(2026, 11, 10, 23, 50, tzinfo=UTC)
    return {
        "project_id": project_id, "run_id": run_id, "ts": ts,
        "last_event_at": ts + datetime.timedelta(minutes=20),
        "conversation_id": "c1", "trigger_source": None,
        "root_entity_type": "application", "root_entity_id": 7,
        "root_entity_version_id": 70, "root_entity_project_id": None,
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
        assert jobs.daily_keys([row, row]) == [(3, "application", 7, datetime.date(2026, 11, 10))]


class TestCalibrationSummary:
    def test_entity_percentiles_need_three_runs(self):
        sample = [
            {"entity_project_id": 3, "root_entity_type": "application", "root_entity_id": 1, "cu_total": v}
            for v in (1, 2, 3)
        ] + [{"entity_project_id": 3, "root_entity_type": "application", "root_entity_id": 2, "cu_total": 50}]
        summary = jobs.calibration_summary(sample, {})
        assert summary["sample_runs"] == 4 and summary["sample_entities"] == 2
        assert summary["entity_percentiles"] == [2.0] * 99
        assert summary["thresholds_source"] == "default"


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

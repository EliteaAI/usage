"""Unit tests for issue #7000: analytics_costs breakdown "Other" row.

Run via: python3 tests/run_tests.py unit/test_7000_analytics_costs_collapse.py -v

_collapse_tail() guarantees:
  - len(rows) <= display_limit  → list returned unchanged.
  - len(rows) >  display_limit  → exactly display_limit+1 rows; last row's
    numeric fields equal the sum of the dropped rows; name_key == other_label.
"""
import usage.api.v2.analytics_costs as _costs_mod
from usage.api.v2.analytics_costs import PromptLibAPI

_MODEL_DISPLAY = _costs_mod._MODEL_DISPLAY
_AGENT_DISPLAY = _costs_mod._AGENT_DISPLAY
_USER_DISPLAY  = _costs_mod._USER_DISPLAY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _model_rows(n, base_cost=100):
    return [
        {
            "model_name": f"model-{i}",
            "calls": 10,
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 1200,
            "cost_nano": base_cost - i,
            "below_resolution": {},
        }
        for i in range(n)
    ]


def _agent_rows(n):
    return [
        {
            "entity_name": f"agent-{i}",
            "root_entity_id": i,
            "root_project_id": 1,
            "calls": 5,
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 600,
            "cost_nano": 50 - i,
            "below_resolution": {},
        }
        for i in range(n)
    ]


def _user_rows(n):
    return [
        {
            "user_id": i,
            "user_email": f"user{i}@example.com",
            "calls": 3,
            "input_tokens": 300,
            "output_tokens": 60,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 360,
            "cost_nano": 30 - i,
            "below_resolution": {},
        }
        for i in range(n)
    ]


def _eval_rows(n):
    return [
        {
            "root_entity_id": i,
            "root_entity_version_id": i * 10,
            "eval_runs": 2,
            "calls": 4,
            "input_tokens": 400,
            "output_tokens": 80,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 480,
            "cost_nano": 40 - i,
            "below_resolution": {},
        }
        for i in range(n)
    ]


def _sum(rows, key):
    return sum(r[key] for r in rows)


# ---------------------------------------------------------------------------
# No-op cases (at or below limit)
# ---------------------------------------------------------------------------

class TestCollapseTailNoOp:
    def test_empty_list(self):
        assert PromptLibAPI._collapse_tail([], 30, "model_name", "__other__") == []

    def test_exactly_at_limit(self):
        rows = _model_rows(30)
        result = PromptLibAPI._collapse_tail(rows, 30, "model_name", "__other__")
        assert result is rows

    def test_below_limit(self):
        rows = _model_rows(5)
        result = PromptLibAPI._collapse_tail(rows, 30, "model_name", "__other__")
        assert result is rows


# ---------------------------------------------------------------------------
# Core collapse behaviour
# ---------------------------------------------------------------------------

class TestCollapseTailOverLimit:
    def test_result_length_is_limit_plus_one(self):
        result = PromptLibAPI._collapse_tail(_model_rows(94), 30, "model_name", "__other__")
        assert len(result) == 31

    def test_other_row_is_appended_last(self):
        result = PromptLibAPI._collapse_tail(_model_rows(94), 30, "model_name", "__other__")
        assert result[-1]["model_name"] == "__other__"

    def test_top_rows_are_unchanged(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30, "model_name", "__other__")
        assert result[:30] == rows[:30]

    def test_other_cost_nano_equals_tail_sum(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30, "model_name", "__other__")
        assert result[-1]["cost_nano"] == _sum(rows[30:], "cost_nano")

    def test_total_cost_nano_is_preserved(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30, "model_name", "__other__")
        assert _sum(result, "cost_nano") == _sum(rows, "cost_nano")

    def test_other_calls_equals_tail_sum(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30, "model_name", "__other__")
        assert result[-1]["calls"] == _sum(rows[30:], "calls")

    def test_other_tokens_equals_tail_sum(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30, "model_name", "__other__")
        assert result[-1]["total_tokens"] == _sum(rows[30:], "total_tokens")

    def test_other_below_resolution_is_empty_dict(self):
        result = PromptLibAPI._collapse_tail(_model_rows(94), 30, "model_name", "__other__")
        assert result[-1]["below_resolution"] == {}

    def test_exactly_one_over_limit(self):
        rows = _model_rows(31)
        result = PromptLibAPI._collapse_tail(rows, 30, "model_name", "__other__")
        assert len(result) == 31
        assert result[-1]["cost_nano"] == rows[30]["cost_nano"]

    def test_all_zero_costs(self):
        rows = [
            {"model_name": f"m{i}", "cost_nano": 0, "calls": 0,
             "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "total_tokens": 0, "below_resolution": {}}
            for i in range(5)
        ]
        result = PromptLibAPI._collapse_tail(rows, 2, "model_name", "__other__")
        assert result[-1]["cost_nano"] == 0
        assert result[-1]["calls"] == 0


# ---------------------------------------------------------------------------
# Sentinel values per breakdown method
# ---------------------------------------------------------------------------

class TestSentinelsByBreakdown:
    def test_by_model_sentinel(self):
        rows = _model_rows(35)
        result = PromptLibAPI._collapse_tail(rows, _MODEL_DISPLAY, "model_name", "__other__")
        assert result[-1]["model_name"] == "__other__"

    def test_by_agent_sentinel(self):
        rows = _agent_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY, "entity_name", "__other__")
        assert len(result) == 21
        assert result[-1]["entity_name"] == "__other__"
        assert result[-1]["cost_nano"] == _sum(rows[20:], "cost_nano")

    def test_by_user_sentinel(self):
        rows = _user_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _USER_DISPLAY, "user_id", "__other__")
        assert len(result) == 21
        assert result[-1]["user_id"] == "__other__"
        assert result[-1]["cost_nano"] == _sum(rows[20:], "cost_nano")

    def test_by_evaluation_sentinel(self):
        rows = _eval_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY, "root_entity_id", "__other__")
        assert len(result) == 21
        assert result[-1]["root_entity_id"] == "__other__"
        assert result[-1]["cost_nano"] == _sum(rows[20:], "cost_nano")


# ---------------------------------------------------------------------------
# Display constants have the expected values from the issue
# ---------------------------------------------------------------------------

class TestDisplayConstants:
    def test_model_display_limit(self):
        assert _MODEL_DISPLAY == 30

    def test_agent_display_limit(self):
        assert _AGENT_DISPLAY == 20

    def test_user_display_limit(self):
        assert _USER_DISPLAY == 20

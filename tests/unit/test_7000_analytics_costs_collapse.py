"""Unit tests for issue #7000: analytics_costs breakdown "Other" row.

Run via: python3 tests/run_tests.py unit/test_7000_analytics_costs_collapse.py -v

_collapse_tail() guarantees:
  - len(rows) <= display_limit  → list returned unchanged.
  - len(rows) >  display_limit  → exactly display_limit+1 rows; last row has
    is_other=True, numeric fields equal the sum of the dropped rows, and
    ID/name fields (root_entity_id, user_id, model_name …) are left None so
    they cannot accidentally match a real record.
  - Decimal values (as Postgres returns for SUM(bigint)) are summed correctly.
"""
from decimal import Decimal

import usage.api.v2.analytics_costs as _costs_mod
from usage.api.v2.analytics_costs import PromptLibAPI

_MODEL_DISPLAY = _costs_mod._MODEL_DISPLAY
_AGENT_DISPLAY = _costs_mod._AGENT_DISPLAY
_USER_DISPLAY  = _costs_mod._USER_DISPLAY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _model_rows(n, use_decimal=False):
    """Rows shaped like what _by_model gets back from fetch_all.

    Pass use_decimal=True to simulate Postgres SUM(bigint) → Decimal, which
    is the production behaviour and the root cause of the original bug.
    """
    def _cost(i):
        v = 100 - i
        return Decimal(v) if use_decimal else v

    return [
        {
            "model_name": f"model-{i}",
            "calls": 10,
            "input_tokens": _cost(i) * 10,
            "output_tokens": _cost(i) * 2,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": _cost(i) * 12,
            "cost_nano": _cost(i),
            "input_cost_nano": _cost(i),
            "output_cost_nano": 0,
            "cache_read_cost_nano": 0,
            "cache_creation_cost_nano": 0,
            "total_cost_sub_nano": 0,
            "input_cost_sub_nano": 0,
            "output_cost_sub_nano": 0,
            "cache_read_cost_sub_nano": 0,
            "cache_creation_cost_sub_nano": 0,
            "below_resolution": {},
        }
        for i in range(n)
    ]


def _agent_rows(n):
    return [
        {
            "entity_name": f"agent-{i}",
            "root_entity_id": i + 100,
            "root_project_id": 1,
            "calls": 5,
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 600,
            "cost_nano": 50 - i,
            "input_cost_nano": 50 - i,
            "output_cost_nano": 0,
            "cache_read_cost_nano": 0,
            "cache_creation_cost_nano": 0,
            "total_cost_sub_nano": 0,
            "input_cost_sub_nano": 0,
            "output_cost_sub_nano": 0,
            "cache_read_cost_sub_nano": 0,
            "cache_creation_cost_sub_nano": 0,
            "below_resolution": {},
        }
        for i in range(n)
    ]


def _user_rows(n):
    return [
        {
            "user_id": i + 200,
            "user_email": f"user{i}@example.com",
            "calls": 3,
            "input_tokens": 300,
            "output_tokens": 60,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 360,
            "cost_nano": 30 - i,
            "input_cost_nano": 30 - i,
            "output_cost_nano": 0,
            "cache_read_cost_nano": 0,
            "cache_creation_cost_nano": 0,
            "total_cost_sub_nano": 0,
            "input_cost_sub_nano": 0,
            "output_cost_sub_nano": 0,
            "cache_read_cost_sub_nano": 0,
            "cache_creation_cost_sub_nano": 0,
            "below_resolution": {},
        }
        for i in range(n)
    ]


def _eval_rows(n):
    return [
        {
            "root_entity_id": i + 300,
            "root_entity_version_id": (i + 300) * 10,
            "eval_runs": 2,
            "calls": 4,
            "input_tokens": 400,
            "output_tokens": 80,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 480,
            "cost_nano": 40 - i,
            "input_cost_nano": 40 - i,
            "output_cost_nano": 0,
            "cache_read_cost_nano": 0,
            "cache_creation_cost_nano": 0,
            "total_cost_sub_nano": 0,
            "input_cost_sub_nano": 0,
            "output_cost_sub_nano": 0,
            "cache_read_cost_sub_nano": 0,
            "cache_creation_cost_sub_nano": 0,
            "below_resolution": {},
        }
        for i in range(n)
    ]


def _isum(rows, key):
    return sum(int(r[key] or 0) for r in rows)


# ---------------------------------------------------------------------------
# No-op cases (at or below limit)
# ---------------------------------------------------------------------------

class TestCollapseTailNoOp:
    def test_empty_list(self):
        assert PromptLibAPI._collapse_tail([], 30) == []

    def test_exactly_at_limit(self):
        rows = _model_rows(30)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert result is rows

    def test_below_limit(self):
        rows = _model_rows(5)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert result is rows


# ---------------------------------------------------------------------------
# Core collapse behaviour
# ---------------------------------------------------------------------------

class TestCollapseTailOverLimit:
    def test_result_length_is_limit_plus_one(self):
        result = PromptLibAPI._collapse_tail(_model_rows(94), 30)
        assert len(result) == 31

    def test_other_row_is_appended_last(self):
        result = PromptLibAPI._collapse_tail(_model_rows(94), 30)
        assert result[-1].get("is_other") is True

    def test_top_rows_are_unchanged(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert result[:30] == rows[:30]

    def test_other_cost_nano_equals_tail_sum(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert result[-1]["cost_nano"] == _isum(rows[30:], "cost_nano")

    def test_total_cost_nano_is_preserved(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert _isum(result, "cost_nano") == _isum(rows, "cost_nano")

    def test_other_calls_equals_tail_sum(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert result[-1]["calls"] == _isum(rows[30:], "calls")

    def test_other_tokens_equals_tail_sum(self):
        rows = _model_rows(94)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert result[-1]["total_tokens"] == _isum(rows[30:], "total_tokens")

    def test_other_below_resolution_is_empty_dict(self):
        result = PromptLibAPI._collapse_tail(_model_rows(94), 30)
        assert result[-1]["below_resolution"] == {}

    def test_exactly_one_over_limit(self):
        rows = _model_rows(31)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert len(result) == 31
        assert result[-1]["cost_nano"] == int(rows[30]["cost_nano"] or 0)

    def test_all_zero_costs(self):
        rows = [
            {
                "model_name": f"m{i}", "calls": 0,
                "cost_nano": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "total_tokens": 0, "below_resolution": {},
                "input_cost_nano": 0, "output_cost_nano": 0,
                "cache_read_cost_nano": 0, "cache_creation_cost_nano": 0,
                "total_cost_sub_nano": 0, "input_cost_sub_nano": 0,
                "output_cost_sub_nano": 0, "cache_read_cost_sub_nano": 0,
                "cache_creation_cost_sub_nano": 0,
            }
            for i in range(5)
        ]
        result = PromptLibAPI._collapse_tail(rows, 2)
        assert result[-1]["cost_nano"] == 0
        assert result[-1]["calls"] == 0


# ---------------------------------------------------------------------------
# Decimal values — Postgres returns SUM(bigint) as Decimal, not int.
# This was the root cause of the original bug: isinstance(Decimal, (int, float))
# is False, so the old code left cost_nano as None → Other row showed $0.
# ---------------------------------------------------------------------------

class TestDecimalSumming:
    def test_decimal_cost_nano_is_summed_correctly(self):
        rows = _model_rows(35, use_decimal=True)
        result = PromptLibAPI._collapse_tail(rows, 30)
        expected = sum(int(rows[i]["cost_nano"]) for i in range(30, 35))
        assert result[-1]["cost_nano"] == expected

    def test_decimal_total_preserved(self):
        rows = _model_rows(35, use_decimal=True)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert _isum(result, "cost_nano") == _isum(rows, "cost_nano")

    def test_decimal_other_row_is_not_zero(self):
        rows = _model_rows(35, use_decimal=True)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert result[-1]["cost_nano"] > 0


# ---------------------------------------------------------------------------
# ID columns must NOT be summed into the Other row
# ---------------------------------------------------------------------------

class TestIdColumnsNotSummed:
    def test_agent_other_row_root_entity_id_is_none(self):
        rows = _agent_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY)
        assert result[-1]["root_entity_id"] is None

    def test_agent_other_row_root_project_id_is_none(self):
        rows = _agent_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY)
        assert result[-1]["root_project_id"] is None

    def test_eval_other_row_root_entity_version_id_is_none(self):
        rows = _eval_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY)
        assert result[-1]["root_entity_version_id"] is None

    def test_user_other_row_user_id_is_none(self):
        rows = _user_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _USER_DISPLAY)
        assert result[-1]["user_id"] is None


# ---------------------------------------------------------------------------
# is_other marker present and correct
# ---------------------------------------------------------------------------

class TestIsOtherMarker:
    def test_other_row_has_is_other_true(self):
        rows = _model_rows(35)
        result = PromptLibAPI._collapse_tail(rows, _MODEL_DISPLAY)
        assert result[-1].get("is_other") is True

    def test_top_rows_do_not_have_is_other(self):
        rows = _model_rows(35)
        result = PromptLibAPI._collapse_tail(rows, _MODEL_DISPLAY)
        for r in result[:_MODEL_DISPLAY]:
            assert not r.get("is_other")

    def test_no_other_when_at_limit(self):
        rows = _model_rows(30)
        result = PromptLibAPI._collapse_tail(rows, 30)
        assert not any(r.get("is_other") for r in result)

    def test_agent_other_row_cost_preserved(self):
        rows = _agent_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY)
        assert result[-1]["cost_nano"] == _isum(rows[_AGENT_DISPLAY:], "cost_nano")

    def test_eval_other_row_eval_runs_summed(self):
        rows = _eval_rows(25)
        result = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY)
        assert result[-1]["eval_runs"] == _isum(rows[_AGENT_DISPLAY:], "eval_runs")

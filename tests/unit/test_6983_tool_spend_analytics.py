"""Analytics -> Costs reports paid tool spend apart from LLM spend (#6983).

tool_cost rows (web search etc.) are listed per feature and backend in by_tool_spend and
never reach the LLM KPIs. Own-key calls are excluded: we cannot know their real rate.
The real SQL runs against an in-memory usage_event.
"""
import datetime
import sys
import types

import pytest

from fixtures.usage_event_table import drop_engine, insert_row, install_engine, load_api_module
from usage.methods import _analytics

PROJECT_ID = 2
TS = datetime.datetime(2026, 10, 8, 12, 0, 0)
WINDOW = (TS - datetime.timedelta(days=2), TS + datetime.timedelta(days=2))


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    yield db_engine
    drop_engine(db_engine)


@pytest.fixture
def costs(monkeypatch, engine):
    monkeypatch.setattr(_analytics, "tool_spend_labels", lambda: {"web_search": "Web search"})
    module = load_api_module(monkeypatch, "analytics_costs")
    yield module
    sys.modules.pop("usage.api.v2.analytics_costs", None)


def row(engine, seconds=0, **values):
    data = {
        "ts": TS + datetime.timedelta(seconds=seconds), "project_id": PROJECT_ID, "user_id": 11,
        "event_type": "tool_cost", "model_name": "tavily/search", "is_error": False,
        "meta": {"feature": "web_search", "backend": "tavily", "op": "search"},
    }
    data.update(values)
    insert_row(engine, **data)


def tool_conditions():
    return _analytics.base_filters(PROJECT_ID, *WINDOW, event_types=(_analytics.EVENT_TOOL_COST,))


class TestToolSpendBreakdown:
    def test_groups_per_feature_and_backend(self, engine, costs):
        row(engine, cost_nano_usd=8_000_000)
        row(engine, seconds=1, cost_nano_usd=16_000_000)
        row(engine, seconds=2, cost_nano_usd=0, is_error=True)
        #
        out = costs.PromptLibAPI._by_tool_spend(tool_conditions())
        assert out == [{
            "feature": "web_search", "label": "Web search", "backend": "tavily", "calls": 3, "failed_calls": 1,
            "total_cost": 0.024,
        }]

    def test_own_key_calls_are_excluded(self, engine, costs):
        # Their real rate is unknown to us, so they are neither costed nor counted
        row(engine, cost_nano_usd=8_000_000)
        row(engine, seconds=1, cost_nano_usd=8_000_000, budget_exempt=True)
        #
        out = costs.PromptLibAPI._by_tool_spend(tool_conditions())[0]
        assert (out["calls"], out["total_cost"]) == (1, 0.008)

    def test_llm_rows_never_reach_tool_spend(self, engine, costs):
        row(engine, event_type="llm", model_name="gpt-x", cost_nano_usd=5_000_000, meta=None)
        assert costs.PromptLibAPI._by_tool_spend(tool_conditions()) == []

    def test_tool_cost_rows_stay_out_of_llm_kpis(self, engine, costs):
        # The LLM tabs keep their numbers: tool_cost is filtered out by event_type
        row(engine, cost_nano_usd=8_000_000)
        llm = _analytics.base_filters(PROJECT_ID, *WINDOW) + [
            _analytics.UsageEvent.event_type == _analytics.EVENT_LLM,
        ]
        assert costs.PromptLibAPI._kpis(llm)["total_cost"] == 0.0

    def test_all_error_group_sorts_last(self, engine, costs):
        # NULL cost on failed calls must not float an all-error group above real spend
        row(engine, cost_nano_usd=8_000_000)
        row(engine, seconds=1, cost_nano_usd=None, is_error=True, meta={"feature": "web_search", "backend": "other"})
        assert [r["backend"] for r in costs.PromptLibAPI._by_tool_spend(tool_conditions())] == ["tavily", "other"]

    def test_unknown_label_falls_back_to_feature_name(self, engine, costs, monkeypatch):
        monkeypatch.setattr(_analytics, "tool_spend_labels", lambda: {})
        row(engine, cost_nano_usd=8_000_000)
        assert costs.PromptLibAPI._by_tool_spend(tool_conditions())[0]["label"] == "web_search"

    def test_other_projects_are_excluded(self, engine, costs):
        row(engine, project_id=PROJECT_ID + 1, cost_nano_usd=8_000_000)
        assert costs.PromptLibAPI._by_tool_spend(tool_conditions()) == []

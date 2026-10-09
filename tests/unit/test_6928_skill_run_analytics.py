"""Per-run skill analytics (#6928).

A skill Run History row is a conversation, and the skill may share it with other participants.
Every run-scoped read takes the skill as its root entity, so only the skill's rows are counted,
and run detail reports the same token total as the Costs and Tokens tabs.
"""
import datetime
import sys

import pytest
from sqlalchemy import func, select

from fixtures.helpers import bind, fake_module
from fixtures.usage_event_table import (
    drop_engine, insert_row, install_engine, load_run_detail, trace_rows,
)
from usage.methods import _analytics
from usage.rpc import analytics as analytics_rpc

PROJECT_ID = 2
CATALOG_PROJECT_ID = 1
SKILL_ID = 174
AGENT_ID = 5
CONVERSATION = "11111111-2222-3333-4444-555555555555"
TS = datetime.datetime(2026, 10, 9, 10, 0, 0)

SKILL_SCOPE = _analytics.RunScope(
    run_id=CONVERSATION, root_entity_type="skill", root_entity_id=SKILL_ID,
    root_entity_project_id=PROJECT_ID,
)


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    yield db_engine
    drop_engine(db_engine)


@pytest.fixture
def run_detail(monkeypatch):
    yield load_run_detail(monkeypatch).PromptLibAPI._build_response
    sys.modules.pop("usage.api.v2.analytics_run_detail", None)


def insert(engine, seconds=0, **values):
    row = {
        "ts": TS + datetime.timedelta(seconds=seconds), "project_id": PROJECT_ID, "user_id": 11,
        "run_id": "turn-1", "conversation_id": CONVERSATION, "event_type": "llm",
        "root_entity_type": "skill", "root_entity_id": SKILL_ID, "root_entity_version_id": 252,
        "root_entity_project_id": PROJECT_ID, "entity_type": "skill", "entity_id": SKILL_ID,
        "entity_name": "summarizer (v2)", "model_name": "sonnet", "is_error": False,
    }
    row.update(values)
    insert_row(engine, **row)


def shared_chat(engine):
    """Two skill turns (one failed), an agent's reply and a Catalog skill sharing the id."""
    insert(engine, billable_input_tokens=100, input_tokens=130, output_tokens=20,
           cache_read_tokens=30, cache_creation_tokens=5, cost_nano_usd=4_000, duration_ms=1_200)
    insert(engine, seconds=60, run_id="turn-2", model_name="gpt", root_entity_version_id=253,
           billable_input_tokens=7, input_tokens=7, cost_nano_usd=50, duration_ms=300, is_error=True)
    insert(engine, seconds=30, run_id="turn-3", root_entity_type="application", root_entity_id=AGENT_ID,
           root_entity_version_id=9, entity_type="application", entity_id=AGENT_ID,
           entity_name="reviewer (base)", model_name="opus", billable_input_tokens=900,
           output_tokens=90, cost_nano_usd=70_000, duration_ms=5_000, is_error=True)
    insert(engine, seconds=40, run_id="turn-4", root_entity_project_id=CATALOG_PROJECT_ID,
           root_entity_version_id=145, model_name="haiku", billable_input_tokens=500,
           cost_nano_usd=9_000, duration_ms=700)
    insert(engine, seconds=50, run_id="turn-3", root_entity_type="application", root_entity_id=AGENT_ID,
           entity_type="application", entity_id=AGENT_ID, event_type="tool", tool_name="search",
           duration_ms=40)


def health(scope):
    return sorted(
        (row["event_type"], row["total"], row["errors"])
        for row in _analytics.event_type_health(PROJECT_ID, run_scope=scope)
    )


class TestParseRunScope:
    def test_the_root_entity_narrows_a_conversation_run(self):
        scope = _analytics.parse_run_scope(PROJECT_ID, {
            "run_id": CONVERSATION, "root_entity_type": "skill", "root_entity_id": "174",
            "root_entity_project_id": "2",
        })

        assert scope == SKILL_SCOPE

    def test_the_root_project_is_optional(self):
        scope = _analytics.parse_run_scope(PROJECT_ID, {
            "run_id": CONVERSATION, "root_entity_type": "skill", "root_entity_id": "174",
        })

        assert (scope.root_entity_id, scope.root_entity_project_id) == (SKILL_ID, None)

    def test_an_agent_run_stays_unnarrowed(self):
        assert _analytics.parse_run_scope(PROJECT_ID, {"run_id": CONVERSATION}) == _analytics.RunScope(
            run_id=CONVERSATION,
        )

    @pytest.mark.parametrize("args", [
        {"root_entity_type": "skill", "root_entity_id": "174"},
        {"run_id": CONVERSATION, "root_entity_type": "skill"},
        {"run_id": CONVERSATION, "root_entity_id": "174"},
        {"run_id": CONVERSATION, "root_entity_type": "toolkit", "root_entity_id": "174"},
        {"run_id": CONVERSATION, "root_entity_type": "skill", "root_entity_id": "abc"},
        {"run_id": CONVERSATION, "root_entity_type": "skill", "root_entity_id": "174",
         "root_entity_project_id": "x"},
    ])
    def test_an_incomplete_or_malformed_root_entity_is_rejected(self, args):
        with pytest.raises(ValueError):
            _analytics.parse_run_scope(PROJECT_ID, args)

    def test_a_malformed_root_entity_is_a_400(self):
        scope, error = _analytics.request_run_scope(PROJECT_ID, {
            "run_id": CONVERSATION, "root_entity_type": "skill", "root_entity_id": "abc",
        })

        assert scope is None
        assert error[1] == 400


class TestRootEntityRunFilter:
    def test_only_the_skill_rows_of_a_shared_conversation_are_counted(self, engine):
        shared_chat(engine)

        assert health(SKILL_SCOPE) == [("llm", 2, 1)]

    def test_without_the_root_entity_the_conversation_counts_every_participant(self, engine):
        shared_chat(engine)

        assert health(_analytics.RunScope(run_id=CONVERSATION)) == [("llm", 4, 2), ("tool", 1, 0)]

    def test_a_catalog_skill_is_told_apart_by_its_project(self, engine):
        shared_chat(engine)
        catalog_scope = SKILL_SCOPE._replace(root_entity_project_id=CATALOG_PROJECT_ID)

        assert health(catalog_scope) == [("llm", 1, 0)]

    def test_a_row_without_a_root_project_belongs_to_the_project_it_ran_in(self, engine):
        insert(engine, root_entity_project_id=None)

        assert health(SKILL_SCOPE) == [("llm", 1, 0)]


class TestHealthRpc:
    def test_the_root_entity_reaches_the_health_scope(self, monkeypatch):
        scopes = []
        monkeypatch.setattr(
            analytics_rpc.an, "event_type_health",
            lambda project_id, dt_from, dt_to, run_scope=None: scopes.append(run_scope) or [],
        )
        rpc = bind(fake_module(), analytics_rpc.RPC)

        rpc.usage_event_type_health(
            PROJECT_ID, run_id=CONVERSATION, root_entity_type="skill", root_entity_id=SKILL_ID,
            root_entity_project_id=PROJECT_ID,
        )

        assert scopes == [SKILL_SCOPE]


class TestSkillRunDetail:
    def detail(self, engine, run_detail, scope=SKILL_SCOPE, display_names=None):
        rows = trace_rows(engine, _analytics, *_analytics.run_filters(scope))
        return run_detail(scope.run_id, rows, display_names or {})

    def test_the_root_is_the_scoped_skill_even_when_another_participant_answered_first(
            self, engine, run_detail,
    ):
        insert(engine, seconds=-10, root_entity_type="application", root_entity_id=AGENT_ID,
               entity_type="application", entity_id=AGENT_ID)
        shared_chat(engine)

        response = self.detail(engine, run_detail)

        assert (response["root_entity_type"], response["root_entity_id"]) == ("skill", SKILL_ID)
        assert response["root_entity_name"] == "summarizer (v2)"
        assert response["sub_agents"] == []

    def test_models_and_versions_are_the_ones_the_run_used_in_order(self, engine, run_detail):
        shared_chat(engine)

        response = self.detail(engine, run_detail, display_names={"sonnet": "Claude Sonnet"})

        assert response["models"] == [
            {"model_name": "sonnet", "display_name": "Claude Sonnet"},
            {"model_name": "gpt", "display_name": "gpt"},
        ]
        assert response["root_entity_version_ids"] == [252, 253]

    def test_totals_are_the_sum_of_the_skill_rows(self, engine, run_detail):
        shared_chat(engine)

        kpis = self.detail(engine, run_detail)["kpis"]

        assert kpis["llm_calls"] == 2
        assert kpis["tool_calls"] == 0
        assert kpis["errors"] == 1
        assert kpis["billable_tokens"] == (100 + 20 + 30 + 5) + 7
        assert kpis["total_cost_nano_usd"] == 4_050
        assert kpis["total_cost"] == pytest.approx(0.00000405)
        assert kpis["calls_duration_ms"] == 1_500

    def test_the_failing_call_is_in_the_trace(self, engine, run_detail):
        shared_chat(engine)

        calls = self.detail(engine, run_detail)["llm_calls"]

        assert [(call["model_name"], call["is_error"], call["billable_tokens"]) for call in calls] == [
            ("sonnet", False, 155), ("gpt", True, 7),
        ]

    def test_billable_tokens_reconcile_with_the_costs_tab(self, engine, run_detail):
        shared_chat(engine)
        costs_conditions = _analytics.base_filters(PROJECT_ID, None, None, run_scope=SKILL_SCOPE) + [
            _analytics.UsageEvent.event_type == _analytics.EVENT_LLM,
        ]
        costs_tokens = _analytics.fetch_all(
            select(func.sum(_analytics.total_tokens_expr()).label("tokens")).where(*costs_conditions)
        )[0]["tokens"]
        kpis = self.detail(engine, run_detail)["kpis"]

        assert kpis["billable_tokens"] == costs_tokens == 162

    def test_an_agent_run_keeps_its_first_row_root_and_trace(self, engine, run_detail):
        shared_chat(engine)
        scope = _analytics.RunScope(run_id="turn-3")

        response = self.detail(engine, run_detail, scope=scope)

        assert (response["root_entity_type"], response["root_entity_id"]) == ("application", AGENT_ID)
        assert response["kpis"]["llm_calls"] == 1
        assert response["kpis"]["tool_calls"] == 1
        assert response["kpis"]["total_tokens"] == 90

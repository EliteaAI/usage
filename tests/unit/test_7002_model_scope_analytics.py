"""Same-named models used both as project-own and as shared stay apart in analytics (#7002).

budget_exempt is TRUE only when the project's own deployment served the call, so it is the
scope analytics groups on. A name used under both scopes gets the scope in its display name;
any other name keeps its plain display name. The real SQL runs against an in-memory usage_event.
"""
import datetime
import sys
import types

import pytest
from sqlalchemy import select

from fixtures.usage_event_table import FakeUsageEvent, drop_engine, insert_row, install_engine, load_api_module
from usage.methods import _analytics
from usage.methods import _skill_analytics as sa

PROJECT_ID = 2
PUBLIC_PROJECT_ID = 1
SKILL_ID = 174
TS = datetime.datetime(2026, 10, 8, 12, 0, 0)
WINDOW = (TS - datetime.timedelta(days=2), TS + datetime.timedelta(days=2))
API_MODULES = ("analytics", "analytics_costs", "analytics_user_detail")

SONNET = "anthropic.claude-sonnet-5"
GPT = "gpt-x"

CONFIGURED = {
    "llm": [
        {"name": SONNET, "display_name": "Dial Sonnet 5", "project_id": PROJECT_ID, "shared": False},
        {"name": SONNET, "display_name": "Sonnet 5", "project_id": PUBLIC_PROJECT_ID, "shared": True},
        {"name": GPT, "display_name": "GPT X", "project_id": PUBLIC_PROJECT_ID, "shared": True},
    ],
}


@pytest.fixture
def rpc(monkeypatch):
    state = {"sections": CONFIGURED, "calls": []}

    class Rpc:
        def timeout(self, _seconds):
            return self

        def configurations_get_models(self, project_id, section, include_shared):
            state["calls"].append((project_id, section, include_shared))
            return {"items": state["sections"].get(section, [])}

    tools = sys.modules["tools"]
    monkeypatch.setattr(tools, "rpc_tools", types.SimpleNamespace(
        RpcMixin=lambda: types.SimpleNamespace(rpc=Rpc()),
    ), raising=False)
    yield state


@pytest.fixture
def engine(monkeypatch, rpc):
    db_engine = install_engine(monkeypatch, _analytics)
    yield db_engine
    drop_engine(db_engine)


@pytest.fixture
def api(monkeypatch, engine):
    modules = types.SimpleNamespace(**{name: load_api_module(monkeypatch, name) for name in API_MODULES})
    yield modules
    for name in API_MODULES:
        sys.modules.pop(f"usage.api.v2.{name}", None)


def call(engine, seconds=0, **values):
    row = {
        "ts": TS + datetime.timedelta(seconds=seconds), "project_id": PROJECT_ID, "user_id": 11,
        "run_id": "run-1", "event_type": "llm", "model_name": SONNET, "is_error": False,
        "root_entity_type": "application", "root_entity_id": 111, "root_entity_project_id": PROJECT_ID,
        "entity_type": "application", "entity_id": 111, "entity_name": "reviewer (base)",
    }
    row.update(values)
    insert_row(engine, **row)


def own_and_shared(engine):
    call(engine, cost_nano_usd=5_000, budget_exempt=True)
    call(engine, seconds=1, cost_nano_usd=1_000, budget_exempt=True)
    call(engine, seconds=2, cost_nano_usd=7_000)
    call(engine, seconds=3, model_name=GPT, cost_nano_usd=300)


def conditions():
    return _analytics.base_filters(PROJECT_ID, *WINDOW)


def by_scope(rows):
    return {(r["model_name"], r["model_scope"]): r for r in rows}


class TestModelDisplayNames:
    def test_each_scope_keeps_its_own_display_name(self, rpc):
        names = _analytics.model_display_names(PROJECT_ID)

        assert names[SONNET] == {"own": "Dial Sonnet 5", "shared": "Sonnet 5"}
        assert names[GPT] == {"shared": "GPT X"}

    def test_shared_configs_are_requested(self, rpc):
        _analytics.model_display_names(PROJECT_ID)

        assert (PROJECT_ID, "llm", True) in rpc["calls"]


class TestModelLabels:
    def test_a_name_configured_under_both_scopes_is_qualified(self, rpc):
        labels = _analytics.model_labels(PROJECT_ID)

        assert labels.label(SONNET, "own") == "Dial Sonnet 5 (project)"
        assert labels.label(SONNET, "shared") == "Sonnet 5 (shared)"

    def test_a_name_in_one_scope_keeps_its_plain_name(self, rpc):
        assert _analytics.model_labels(PROJECT_ID).label(GPT, "shared") == "GPT X"

    def test_rows_under_both_scopes_qualify_a_name_configured_once(self, rpc):
        rpc["sections"] = {"llm": [CONFIGURED["llm"][2]]}
        rows = [{"model_name": GPT, "model_scope": "own"}, {"model_name": GPT, "model_scope": "shared"}]

        labels = _analytics.model_labels(PROJECT_ID, rows)

        assert labels.label(GPT, "own") == "GPT X (project)"
        assert labels.label(GPT, "shared") == "GPT X (shared)"

    def test_an_unconfigured_name_falls_back_to_itself(self, rpc):
        labels = _analytics.model_labels(PROJECT_ID)

        assert labels.label("never-configured", "shared") == "never-configured"
        assert labels.get("never-configured") is None

    def test_a_scope_with_no_config_borrows_the_other_scopes_name(self, rpc):
        rpc["sections"] = {"llm": [CONFIGURED["llm"][2]]}
        rows = [{"model_name": GPT, "model_scope": "own"}, {"model_name": GPT, "model_scope": "shared"}]

        assert _analytics.model_labels(PROJECT_ID, rows).label(GPT, "own") == "GPT X (project)"

    def test_unavailable_configurations_keep_raw_names(self, monkeypatch):
        def broken():
            raise RuntimeError("down")

        tools = sys.modules["tools"]
        monkeypatch.setattr(tools, "rpc_tools", types.SimpleNamespace(RpcMixin=broken), raising=False)

        labels = _analytics.model_labels(PROJECT_ID)

        assert not labels
        assert labels.label(SONNET, "own") == SONNET


class TestModelScopeExpr:
    def test_null_budget_exempt_reads_as_shared(self, engine):
        call(engine)
        call(engine, seconds=1, budget_exempt=False)
        call(engine, seconds=2, budget_exempt=True)

        rows = _analytics.fetch_all(
            select(_analytics.model_scope_expr().label("scope")).order_by(FakeUsageEvent.ts)
        )

        assert [r["scope"] for r in rows] == ["shared", "shared", "own"]


class TestCostsByModel:
    def test_rows_carry_scope_and_qualified_name(self, engine, api):
        own_and_shared(engine)

        rows = by_scope(api.analytics_costs.PromptLibAPI._by_model(PROJECT_ID, conditions()))

        assert rows[(SONNET, "own")]["calls"] == 2
        assert rows[(SONNET, "own")]["display_name"] == "Dial Sonnet 5 (project)"
        assert rows[(SONNET, "shared")]["calls"] == 1
        assert rows[(SONNET, "shared")]["display_name"] == "Sonnet 5 (shared)"
        assert rows[(GPT, "shared")]["display_name"] == "GPT X"

    def test_scope_cost_is_not_merged(self, engine, api):
        own_and_shared(engine)

        rows = by_scope(api.analytics_costs.PromptLibAPI._by_model(PROJECT_ID, conditions()))

        assert rows[(SONNET, "own")]["total_cost"] == _analytics.cost_usd(6_000)
        assert rows[(SONNET, "shared")]["total_cost"] == _analytics.cost_usd(7_000)


class TestOverviewModels:
    def test_own_and_shared_are_separate_rows(self, engine, api):
        own_and_shared(engine)

        rows = by_scope(api.analytics.PromptLibAPI._models(PROJECT_ID, conditions()))

        assert rows[(SONNET, "own")]["calls"] == 2
        assert rows[(SONNET, "shared")]["calls"] == 1
        assert rows[(SONNET, "own")]["display_name"] != rows[(SONNET, "shared")]["display_name"]

    def test_a_model_used_in_one_scope_keeps_its_name(self, engine, api):
        call(engine, budget_exempt=True)

        rows = api.analytics.PromptLibAPI._models(PROJECT_ID, conditions())

        assert [(r["model_scope"], r["display_name"]) for r in rows] == [("own", "Dial Sonnet 5 (project)")]


class TestUserDetailModels:
    def test_own_and_shared_are_separate_rows(self, engine, api):
        own_and_shared(engine)

        rows = by_scope(api.analytics_user_detail.PromptLibAPI._models(PROJECT_ID, conditions()))

        assert rows[(SONNET, "own")]["display_name"] == "Dial Sonnet 5 (project)"
        assert rows[(SONNET, "shared")]["display_name"] == "Sonnet 5 (shared)"


class TestSkillDetailModels:
    def test_skill_model_breakdown_splits_by_scope(self, engine, monkeypatch):
        monkeypatch.setattr(sa, "fetch_skill_meta", lambda project_id, skill_refs=(), conversation_uuids=(): ({}, {}))
        monkeypatch.setattr(_analytics, "application_meta", lambda project_id, refs=(): {})
        monkeypatch.setattr(_analytics, "label_users", lambda user_ids, stored=None: {})
        skill = {
            "root_entity_type": "skill", "root_entity_id": SKILL_ID, "entity_type": "skill",
            "entity_id": SKILL_ID, "entity_name": "summarizer (base)", "root_entity_version_id": 252,
        }
        call(engine, budget_exempt=True, **skill)
        call(engine, seconds=1, run_id="run-2", **skill)

        detail = sa.build_skill_detail(PROJECT_ID, SKILL_ID, PROJECT_ID, *WINDOW)

        assert sorted((m["model_scope"], m["display_name"]) for m in detail["by_model"]) == [
            ("own", "Dial Sonnet 5 (project)"), ("shared", "Sonnet 5 (shared)"),
        ]
        assert sorted(r["model_name"] for r in detail["recent_runs"]) == [
            "Dial Sonnet 5 (project)", "Sonnet 5 (shared)",
        ]


class TestUsagePageLabels:
    def test_usage_rows_resolve_through_scoped_labels(self, rpc):
        from usage.api.v2 import usage  # pylint: disable=C0415

        rows = usage._attach_display_names([{"model": GPT}, {"model": "bedrock/" + GPT}], usage._model_display_names(PROJECT_ID))

        assert [r["display_name"] for r in rows] == ["GPT X", "GPT X"]

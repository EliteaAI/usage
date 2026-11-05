"""The Usage page tells pipeline runs from agent runs (#7413).

usage_event always writes root_entity_type='application', pipelines included, so Spend by source
had no Pipelines slice and Top Spend Agents/Pipelines badged every pipeline AG. The kind comes from
elitea_core's usage_entity_meta (ApplicationVersion.agent_type), keyed by the root's own schema.
The real expressions run against the in-memory usage_event.
"""
import datetime
import sys
import types

import pytest
from sqlalchemy import func, select

from fixtures.usage_event_table import FAKE_TABLE, FakeUsageEvent, drop_engine, insert_row, install_engine
from usage.methods import _analytics

PROJECT_ID = 32
PUBLIC_PROJECT_ID = 1
PIPELINE_ID = 545
AGENT_ID = 544
TS = datetime.datetime(2026, 11, 4, 12, 0, 0)

KINDS = {
    (PROJECT_ID, PIPELINE_ID): "pipeline",
    (PROJECT_ID, AGENT_ID): "agent",
    # Same id as the local pipeline, but a public agent: must stay "agents"
    (PUBLIC_PROJECT_ID, PIPELINE_ID): "agent",
}


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    calls = []

    def entity_meta_rpc(project_id, **kwargs):
        calls.append(kwargs)
        return {"scoped_applications": [
            {"project_id": p, "id": i, "name": f"app {i}", "kind": KINDS[(p, i)]}
            for p, i in kwargs.get("application_refs") or [] if (p, i) in KINDS
        ]}

    monkeypatch.setattr(_analytics, "_entity_meta_rpc", entity_meta_rpc)
    db_engine.rpc_calls = calls
    yield db_engine
    drop_engine(db_engine)


def run(db_engine, run_id, app_id, cost, root_project=PROJECT_ID, **values):
    """One llm call of a run of app_id: the run row itself, as every row under a run carries its root."""
    row = {
        "ts": TS, "project_id": PROJECT_ID, "user_id": 7, "run_id": run_id, "event_type": "llm",
        "root_entity_type": "application", "root_entity_id": app_id,
        "root_entity_project_id": root_project, "entity_type": "application", "entity_id": app_id,
        "entity_name": f"app {app_id} (1)", "cost_nano_usd": cost, "is_error": False,
    }
    row.update(values)
    insert_row(db_engine, **row)


def buckets(db_engine, pipeline_refs):
    label = _analytics.source_bucket_expr(pipeline_refs).label("bucket")
    statement = select(label, func.sum(FakeUsageEvent.cost_nano_usd).label("cost")).select_from(
        FAKE_TABLE,
    ).group_by(label)
    return {row["bucket"]: row["cost"] for row in _analytics.fetch_all(statement)}


def seed(db_engine):
    run(db_engine, "p1", PIPELINE_ID, 300)
    run(db_engine, "a1", AGENT_ID, 100)
    run(db_engine, "pub", PIPELINE_ID, 20, root_project=PUBLIC_PROJECT_ID)
    run(db_engine, "sched", PIPELINE_ID, 50, trigger_source="scheduled")
    run(db_engine, "eval", PIPELINE_ID, 7, entity_type="evaluation")
    insert_row(db_engine, ts=TS, project_id=PROJECT_ID, user_id=7, run_id="s1", event_type="llm",
               root_entity_type="skill", root_entity_id=PIPELINE_ID, root_entity_project_id=PROJECT_ID,
               entity_type="skill", entity_id=PIPELINE_ID, cost_nano_usd=5, is_error=False)


class TestPipelineRootRefs:
    def test_only_pipelines_are_returned_keyed_by_their_own_schema(self, engine):
        seed(engine)
        assert _analytics.pipeline_root_refs(PROJECT_ID, []) == [(PROJECT_ID, PIPELINE_ID)]

    def test_skill_rows_are_never_looked_up_as_applications(self, engine):
        # Skill and application ids come from separate sequences, so a skill can share an app's id
        seed(engine)
        _analytics.pipeline_root_refs(PROJECT_ID, [])
        (call,) = engine.rpc_calls
        assert sorted(map(tuple, call["application_refs"])) == [
            (PUBLIC_PROJECT_ID, PIPELINE_ID), (PROJECT_ID, AGENT_ID), (PROJECT_ID, PIPELINE_ID),
        ]

    def test_conditions_scope_the_lookup(self, engine):
        seed(engine)
        assert _analytics.pipeline_root_refs(PROJECT_ID, [FakeUsageEvent.run_id == "a1"]) == []

    def test_no_application_rows_is_an_empty_list(self, engine):
        assert _analytics.pipeline_root_refs(PROJECT_ID, []) == []

    def test_a_failed_lookup_degrades_to_no_pipelines(self, engine, monkeypatch, recording_log):
        seed(engine)

        def broken(_statement):
            raise RuntimeError("no database")

        monkeypatch.setattr(_analytics, "fetch_all", broken)
        assert _analytics.pipeline_root_refs(PROJECT_ID, []) == []
        assert recording_log.messages("warning")

    def test_an_unreachable_elitea_core_degrades_to_no_pipelines(self, engine, monkeypatch):
        seed(engine)
        monkeypatch.setattr(_analytics, "_entity_meta_rpc", lambda project_id, **kwargs: {})
        assert _analytics.pipeline_root_refs(PROJECT_ID, []) == []


class TestSourceBucket:
    def test_pipeline_runs_get_their_own_slice(self, engine):
        seed(engine)
        result = buckets(engine, _analytics.pipeline_root_refs(PROJECT_ID, []))
        assert result["pipelines"] == 300
        # The local agent plus the public same-id agent
        assert result["agents"] == 120

    def test_automations_evaluations_and_skills_keep_their_buckets(self, engine):
        seed(engine)
        result = buckets(engine, _analytics.pipeline_root_refs(PROJECT_ID, []))
        assert result["automations"] == 50
        assert result["evaluations"] == 7
        assert result["skills"] == 5

    def test_the_total_is_unchanged_only_relabelled(self, engine):
        seed(engine)
        before = buckets(engine, [])
        after = buckets(engine, _analytics.pipeline_root_refs(PROJECT_ID, []))
        assert sum(before.values()) == sum(after.values())
        assert before["agents"] == after["agents"] + after["pipelines"]

    def test_without_refs_the_buckets_are_the_previous_ones(self, engine):
        seed(engine)
        assert buckets(engine, []) == {"agents": 420, "automations": 50, "evaluations": 7, "skills": 5}

    def test_a_row_already_written_as_pipeline_is_still_a_pipeline(self, engine):
        run(engine, "native", 9, 11, root_entity_type="pipeline", entity_type="pipeline")
        assert buckets(engine, []) == {"pipelines": 11}


class TestApplicationKind:
    def test_pipeline_kind_overrides_the_stored_type(self):
        applications = {(PROJECT_ID, PIPELINE_ID): {"kind": "pipeline"}}
        assert _analytics.application_kind(applications, PROJECT_ID, PIPELINE_ID, "application") == "pipeline"

    def test_an_agent_or_unknown_entity_keeps_the_stored_type(self):
        applications = {(PROJECT_ID, AGENT_ID): {"kind": "agent"}}
        assert _analytics.application_kind(applications, PROJECT_ID, AGENT_ID, "application") == "application"
        assert _analytics.application_kind({}, PROJECT_ID, PIPELINE_ID, "application") == "application"


@pytest.fixture
def usage_api(engine, monkeypatch):
    """api/v2/usage.py as the plugin package's module, with flask and the tools members it needs stubbed."""
    import importlib  # pylint: disable=C0415

    import usage.models.usage_event as usage_event_model  # pylint: disable=C0415

    tools = sys.modules["tools"]
    monkeypatch.setattr(tools, "api_tools", types.SimpleNamespace(
        APIModeHandler=object, APIBase=object, with_modes=lambda params: params,
        endpoint_metrics=lambda f: f,
    ), raising=False)
    monkeypatch.setattr(tools, "auth", types.SimpleNamespace(
        decorators=types.SimpleNamespace(check_api=lambda *a, **k: (lambda f: f)),
        current_user=lambda: {"id": 7},
    ), raising=False)
    monkeypatch.setattr(tools, "register_openapi", lambda *a, **k: (lambda f: f), raising=False)
    monkeypatch.setattr(tools, "rpc_tools", types.SimpleNamespace(RpcMixin=object), raising=False)
    monkeypatch.setattr(tools.config, "DEFAULT_MODE", "default", raising=False)
    flask_stub = types.ModuleType("flask")
    flask_stub.request = types.SimpleNamespace(args={})
    monkeypatch.setitem(sys.modules, "flask", flask_stub)
    monkeypatch.delitem(sys.modules, "usage.api.v2.usage", raising=False)
    # The query helpers import UsageEvent from the model module at call time
    monkeypatch.setattr(usage_event_model, "UsageEvent", FakeUsageEvent)
    yield importlib.import_module("usage.api.v2.usage")
    sys.modules.pop("usage.api.v2.usage", None)


WINDOW = (TS - datetime.timedelta(days=1), TS + datetime.timedelta(days=1))


class TestTopAgents:
    @pytest.mark.parametrize("sort_by", ["spend", "runs"])
    def test_pipelines_are_typed_pipeline_and_agents_keep_application(self, usage_api, engine, sort_by):
        run(engine, "p1", PIPELINE_ID, 300)
        run(engine, "p2", PIPELINE_ID, 300)
        run(engine, "a1", AGENT_ID, 100)
        rows = usage_api._usage_top_agents(PROJECT_ID, *WINDOW, sort_by=sort_by)
        assert {r["id"]: r["type"] for r in rows} == {PIPELINE_ID: "pipeline", AGENT_ID: "application"}

    def test_a_public_agent_sharing_the_pipeline_id_stays_an_agent(self, usage_api, engine):
        run(engine, "p1", PIPELINE_ID, 300)
        run(engine, "pub", PIPELINE_ID, 20, root_project=PUBLIC_PROJECT_ID)
        rows = usage_api._usage_top_agents(PROJECT_ID, *WINDOW)
        assert {(r["root_project_id"], r["type"]) for r in rows} == {
            (PROJECT_ID, "pipeline"), (PUBLIC_PROJECT_ID, "application"),
        }

    def test_an_unreachable_elitea_core_falls_back_to_the_stored_type(self, usage_api, engine, monkeypatch):
        run(engine, "p1", PIPELINE_ID, 300)
        monkeypatch.setattr(_analytics, "_entity_meta_rpc", lambda project_id, **kwargs: {})
        rows = usage_api._usage_top_agents(PROJECT_ID, *WINDOW)
        assert [r["type"] for r in rows] == ["application"]


class TestDailyBreakdown:
    def test_the_source_series_has_a_pipelines_key(self, usage_api, engine, monkeypatch):
        # date_trunc is Postgres-only; the day itself is not what this test is about
        monkeypatch.setattr(usage_api, "func", _SqliteFunc(), raising=False)
        run(engine, "p1", PIPELINE_ID, 300)
        run(engine, "a1", AGENT_ID, 100)
        rows = usage_api._usage_daily_breakdown(PROJECT_ID, *WINDOW)
        sources = {r["key"]: r["spend"] for r in rows if r["dimension"] == "source"}
        assert set(sources) == {"pipelines", "agents"}
        assert sources["pipelines"] == _analytics.cost_usd(300)


class _SqliteFunc:
    """sqlalchemy.func with date_trunc('day', ts) rendered as a SQLite datetime of the day start."""

    def __getattr__(self, name):
        if name == "date_trunc":
            return lambda _unit, column: func.datetime(func.date(column), type_=FakeUsageEvent.ts.type)
        return getattr(func, name)

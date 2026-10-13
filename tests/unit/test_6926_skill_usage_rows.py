"""Skill usage rows (#6926) must not move any existing KPI.

An in-agent skill activation is written as event_type='skill' with zero tokens and cost and
tool_name NULL. base_filters keeps those rows out of every aggregate unless a caller opts in,
run detail lists them as skill events, and a skill whose id collides with its agent's never
names the agent's run. The real SQL expressions run against an in-memory table.
"""
import datetime
import sys

import pytest
from sqlalchemy import distinct, func, select

from fixtures.usage_event_table import (
    FakeUsageEvent, drop_engine, insert_row, install_engine, load_run_detail, trace_rows,
)
from usage.methods import _analytics

PROJECT_ID = 7
AGENT_ID = 5
TS = datetime.datetime(2026, 10, 8, 12, 0, 0)


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    yield db_engine
    drop_engine(db_engine)


def insert(engine, **values):
    row = {
        "ts": TS, "project_id": PROJECT_ID, "user_id": 11, "run_id": "run-1",
        "conversation_id": "conv-1", "root_entity_type": "application", "root_entity_id": AGENT_ID,
        "entity_type": "application", "entity_id": AGENT_ID, "entity_name": "my-agent (base)",
        "is_error": False,
    }
    row.update(values)
    insert_row(engine, **row)


def agent_fixture(engine):
    """Two agent runs, one with a failed tool, a load_skill tool row and its llm calls."""
    insert(engine, event_type="llm", model_name="gpt", billable_input_tokens=100, output_tokens=20,
           cost_nano_usd=900, duration_ms=1000)
    insert(engine, event_type="tool", tool_name="load_skill", duration_ms=5)
    insert(engine, event_type="llm", model_name="gpt", billable_input_tokens=300, output_tokens=40,
           cost_nano_usd=2100, duration_ms=3000)
    insert(engine, run_id="run-2", event_type="tool", tool_name="jira_search", duration_ms=50,
           is_error=True)
    insert(engine, run_id="run-2", event_type="llm", model_name="gpt", billable_input_tokens=10,
           output_tokens=5, cost_nano_usd=70, duration_ms=200)


def skill_rows(engine):
    """What the indexer adds for the same runs: a loaded skill sharing the agent's id, a mention
    and an unknown load_skill name."""
    insert(engine, event_type="skill", entity_type="skill", entity_id=AGENT_ID, entity_version_id=40,
           entity_name="pdf-skill", meta={"source": "load_skill", "outcome": "loaded"})
    insert(engine, event_type="skill", entity_type="skill", entity_id=9, entity_version_id=41,
           entity_name="tone", meta={"source": "mention", "outcome": "loaded"})
    insert(engine, run_id="run-2", event_type="skill", entity_type="skill", entity_id=None,
           entity_name="nope", meta={"source": "load_skill", "outcome": "unknown_skill"})


def kpis():
    """The agent, tool, user and health figures the analytics endpoints compute."""
    conditions = _analytics.base_filters(PROJECT_ID, None, None, human_only=False)
    totals = _analytics.fetch_one(select(
        _analytics.agent_runs_expr().label("agent_runs"),
        _analytics.agent_error_runs_expr().label("error_runs"),
        func.sum(_analytics.total_tokens_expr()).label("tokens"),
        func.sum(FakeUsageEvent.cost_nano_usd).label("cost"),
        func.count().label("total_events"),
        func.count(distinct(func.nullif(FakeUsageEvent.tool_name, ""))).label("unique_tools"),
        func.avg(FakeUsageEvent.duration_ms).label("avg_duration_ms"),
        _analytics.llm_calls_expr().label("llm_calls"),
        _analytics.tool_runs_expr().label("tool_runs"),
    ).where(*conditions))
    leaderboard = _analytics.fetch_all(select(
        FakeUsageEvent.tool_name, func.count().label("calls"),
    ).where(*conditions, FakeUsageEvent.tool_name.isnot(None)).group_by(FakeUsageEvent.tool_name)
        .order_by(FakeUsageEvent.tool_name))
    name = _analytics.fetch_one(select(_analytics.run_name_expr().label("name")).where(
        *conditions, FakeUsageEvent.root_entity_id == AGENT_ID,
    ))
    return {
        **dict(totals),
        "leaderboard": [dict(r) for r in leaderboard],
        "health": sorted(
            (h["event_type"], h["total"], h["errors"], h["avg_duration_ms"])
            for h in _analytics.event_type_health(PROJECT_ID, TS - datetime.timedelta(days=1), TS)
        ),
        "agent_name": name["name"],
    }


class TestKpisUnchangedBySkillRows:
    def test_every_kpi_is_identical_before_and_after_skill_rows(self, engine):
        agent_fixture(engine)
        before = kpis()
        #
        skill_rows(engine)
        #
        assert kpis() == before

    def test_fixture_is_not_trivially_empty(self, engine):
        agent_fixture(engine)
        #
        result = kpis()
        #
        assert result["agent_runs"] == 2
        assert result["error_runs"] == 1
        assert result["leaderboard"] == [
            {"tool_name": "jira_search", "calls": 1}, {"tool_name": "load_skill", "calls": 1},
        ]
        assert result["agent_name"] == "my-agent"

    def test_health_has_no_skill_bucket(self, engine):
        agent_fixture(engine)
        skill_rows(engine)
        #
        buckets = {h["event_type"] for h in _analytics.event_type_health(
            PROJECT_ID, TS - datetime.timedelta(days=1), TS,
        )}
        #
        assert buckets == {"llm", "tool"}

    def test_run_scoped_health_has_no_skill_bucket(self, engine):
        agent_fixture(engine)
        skill_rows(engine)
        #
        buckets = {h["event_type"] for h in _analytics.event_type_health(
            PROJECT_ID, run_scope=_analytics.RunScope(run_id="run-1"),
        )}
        #
        assert buckets == {"llm", "tool"}


class TestBaseFiltersEventTypes:
    def _types(self, **kwargs):
        rows = _analytics.fetch_all(select(distinct(FakeUsageEvent.event_type)).where(
            *_analytics.base_filters(PROJECT_ID, None, None, human_only=False, **kwargs),
        ))
        return {r["event_type"] for r in rows}

    def test_skill_rows_are_left_out_by_default(self, engine):
        agent_fixture(engine)
        skill_rows(engine)
        #
        assert self._types() == {"llm", "tool"}

    def test_a_caller_opts_in_to_skill_rows(self, engine):
        agent_fixture(engine)
        skill_rows(engine)
        #
        assert self._types(event_types=(_analytics.EVENT_SKILL,)) == {"skill"}


class TestSkillRootRuns:
    def test_skill_root_run_is_not_an_agent_run(self, engine):
        insert(engine, run_id="skill-run", root_entity_type="skill", root_entity_id=3,
               entity_type="skill", entity_id=3, entity_name="pdf-skill", event_type="llm",
               billable_input_tokens=50, cost_nano_usd=100)
        #
        result = kpis()
        #
        assert result["agent_runs"] == 0
        assert result["tokens"] == 50
        assert result["health"] == [("llm", 1, 0, None)]


class TestRunNameIsTypeAware:
    def test_a_skill_sharing_the_agents_id_never_names_the_run(self, engine):
        insert(engine, event_type="skill", entity_type="skill", entity_id=AGENT_ID,
               entity_name="zzz-skill")
        insert(engine, event_type="llm", entity_name="my-agent (base)")
        #
        name = _analytics.fetch_one(select(_analytics.run_name_expr().label("name")).where(
            FakeUsageEvent.root_entity_id == AGENT_ID,
        ))
        #
        assert name["name"] == "my-agent"

    def test_an_evaluation_row_sharing_the_agents_id_never_names_the_run(self, engine):
        insert(engine, event_type="llm", entity_type="evaluation", entity_id=AGENT_ID,
               entity_name="zzz-eval")
        insert(engine, event_type="llm", entity_name="my-agent (base)")
        #
        name = _analytics.fetch_one(select(_analytics.run_name_expr().label("name")))
        #
        assert name["name"] == "my-agent"


@pytest.fixture
def run_detail(monkeypatch):
    yield load_run_detail(monkeypatch).PromptLibAPI._build_response
    sys.modules.pop("usage.api.v2.analytics_run_detail", None)


def detail_rows(engine, *conditions):
    return trace_rows(engine, _analytics, *conditions)


class TestRunDetail:
    def test_skill_events_are_listed_and_never_sub_agents(self, engine, run_detail):
        agent_fixture(engine)
        skill_rows(engine)
        insert(engine, event_type="llm", entity_id=31, entity_name="child-agent")
        #
        response = run_detail("run-1", detail_rows(engine, FakeUsageEvent.run_id == "run-1"))
        #
        assert response["root_entity_name"] == "my-agent (base)"
        assert [s["entity_id"] for s in response["sub_agents"]] == [31]
        assert response["skill_events"] == [
            {"entity_id": AGENT_ID, "entity_version_id": 40, "entity_name": "pdf-skill",
             "source": "load_skill", "outcome": "loaded", "parent_agent_name": None,
             "ts": TS.isoformat()},
            {"entity_id": 9, "entity_version_id": 41, "entity_name": "tone", "source": "mention",
             "outcome": "loaded", "parent_agent_name": None, "ts": TS.isoformat()},
        ]
        assert response["kpis"]["skill_events"] == 2

    def test_skill_rows_leave_the_run_kpis_unchanged(self, engine, run_detail):
        agent_fixture(engine)
        before = run_detail("run-1", detail_rows(engine, FakeUsageEvent.run_id == "run-1"))
        skill_rows(engine)
        #
        after = run_detail("run-1", detail_rows(engine, FakeUsageEvent.run_id == "run-1"))
        #
        assert after["kpis"] == {**before["kpis"], "skill_events": 2}
        assert after["llm_calls"] == before["llm_calls"]
        assert after["tool_calls"] == before["tool_calls"]
        assert after["sub_agents"] == before["sub_agents"] == []

    def test_a_skill_root_run_has_no_sub_agents(self, engine, run_detail):
        insert(engine, run_id="skill-run", root_entity_type="skill", root_entity_id=3,
               entity_type="skill", entity_id=3, entity_name="pdf-skill", event_type="llm")
        #
        response = run_detail("skill-run", detail_rows(engine))
        #
        assert response["root_entity_type"] == "skill"
        assert response["root_entity_name"] == "pdf-skill"
        assert response["sub_agents"] == []
        assert response["kpis"]["llm_calls"] == 1

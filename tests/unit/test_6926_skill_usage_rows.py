"""Skill usage rows (#6926) must not move any existing KPI.

An in-agent skill activation is written as event_type='skill' with zero tokens and cost and
tool_name NULL. base_filters keeps those rows out of every aggregate unless a caller opts in,
run detail lists them as skill events, and a skill whose id collides with its agent's never
names the agent's run. The real SQL expressions run against an in-memory table.
"""
import datetime
import importlib
import re
import sys
import types

import pytest
from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, create_engine, distinct,
    event, func, select,
)

from usage.methods import _analytics, skill_index
from usage.models.usage_event import SKILL_INDEX_NAME, SKILL_INDEX_PREDICATE, UsageEvent

METADATA = MetaData()

FAKE_TABLE = Table(
    "usage_event", METADATA,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime),
    Column("project_id", Integer),
    Column("user_id", Integer),
    Column("user_email", String),
    Column("run_id", String),
    Column("conversation_id", String),
    Column("root_entity_type", String),
    Column("root_entity_id", Integer),
    Column("root_entity_version_id", Integer),
    Column("root_entity_project_id", Integer),
    Column("entity_type", String),
    Column("entity_id", Integer),
    Column("entity_version_id", Integer),
    Column("entity_name", String),
    Column("event_type", String),
    Column("model_name", String),
    Column("tool_name", String),
    Column("input_tokens", Integer, default=0),
    Column("output_tokens", Integer, default=0),
    Column("billable_input_tokens", Integer, default=0),
    Column("cache_read_tokens", Integer, default=0),
    Column("cache_creation_tokens", Integer, default=0),
    Column("cost_nano_usd", Integer, default=0),
    Column("duration_ms", Integer),
    Column("is_error", Boolean, default=False),
    Column("trigger_source", String),
    Column("meta", JSON),
)

FakeUsageEvent = types.SimpleNamespace(**{column.name: column for column in FAKE_TABLE.c})

PROJECT_ID = 7
AGENT_ID = 5
TS = datetime.datetime(2026, 10, 8, 12, 0, 0)


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(_analytics, "UsageEvent", FakeUsageEvent)
    monkeypatch.setattr(_analytics, "HUMAN_ACTOR", FakeUsageEvent.user_id != _analytics.SYSTEM_USER_ID)
    db_engine = create_engine("sqlite://")
    event.listen(db_engine, "connect", lambda connection, _record: connection.create_function(
        "regexp_replace", 3, lambda value, pattern, repl: None if value is None else re.sub(pattern, repl, value),
    ))
    METADATA.create_all(db_engine)

    def fetch_all(statement):
        with db_engine.connect() as connection:
            return list(connection.execute(statement).mappings())

    monkeypatch.setattr(_analytics, "fetch_all", fetch_all)
    yield db_engine
    METADATA.drop_all(db_engine)


def insert(engine, **values):
    row = {
        "ts": TS, "project_id": PROJECT_ID, "user_id": 11, "run_id": "run-1",
        "conversation_id": "conv-1", "root_entity_type": "application", "root_entity_id": AGENT_ID,
        "entity_type": "application", "entity_id": AGENT_ID, "entity_name": "my-agent (base)",
        "is_error": False,
    }
    row.update(values)
    with engine.begin() as connection:
        connection.execute(FAKE_TABLE.insert().values(**row))


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
    tools = sys.modules["tools"]
    monkeypatch.setattr(tools, "api_tools", types.SimpleNamespace(
        APIModeHandler=object, APIBase=object, with_modes=lambda params: params,
        endpoint_metrics=lambda func: func,
    ), raising=False)
    monkeypatch.setattr(tools, "auth", types.SimpleNamespace(
        decorators=types.SimpleNamespace(check_api=lambda *a, **k: (lambda func: func)),
    ), raising=False)
    monkeypatch.setattr(tools.config, "DEFAULT_MODE", "default", raising=False)
    monkeypatch.setattr(tools, "register_openapi", lambda *a, **k: (lambda func: func), raising=False)
    flask_stub = types.ModuleType("flask")
    flask_stub.request = types.SimpleNamespace(args={})
    monkeypatch.setitem(sys.modules, "flask", flask_stub)
    monkeypatch.delitem(sys.modules, "usage.api.v2.analytics_run_detail", raising=False)
    module = importlib.import_module("usage.api.v2.analytics_run_detail")
    yield module.PromptLibAPI._build_response
    sys.modules.pop("usage.api.v2.analytics_run_detail", None)


def detail_rows(engine):
    with engine.connect() as connection:
        return list(connection.execute(select(FAKE_TABLE).order_by(FAKE_TABLE.c.seq)).mappings())


class TestRunDetail:
    def test_skill_events_are_listed_and_never_sub_agents(self, engine, run_detail):
        agent_fixture(engine)
        skill_rows(engine)
        insert(engine, event_type="llm", entity_id=31, entity_name="child-agent")
        #
        response = run_detail("run-1", [r for r in detail_rows(engine) if r["run_id"] == "run-1"])
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
        before = run_detail("run-1", [r for r in detail_rows(engine) if r["run_id"] == "run-1"])
        skill_rows(engine)
        #
        after = run_detail("run-1", [r for r in detail_rows(engine) if r["run_id"] == "run-1"])
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


class TestSkillIndexModel:
    def test_model_declares_the_partial_skill_index(self):
        index = next(i for i in UsageEvent.__table__.indexes if i.name == SKILL_INDEX_NAME)
        #
        assert [c.name for c in index.columns] == ["project_id", "entity_id", "ts"]
        assert str(index.dialect_options["postgresql"]["where"]) == SKILL_INDEX_PREDICATE


class FakeConnection:
    """Answers the catalog queries from a dict and records every DDL statement."""

    def __init__(self, partitions, attached=(), invalid=()):
        self.partitions = list(partitions)
        self.attached = set(attached)
        self.invalid = set(invalid)
        self.ddl = []
        self.isolation_level = None

    def execution_options(self, isolation_level=None):
        self.isolation_level = isolation_level
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        if sql == str(skill_index._PARTITIONS_SQL):
            return [(p,) for p in self.partitions]
        if sql == str(skill_index._ATTACHED_SQL):
            return Result(params["partition"] in self.attached)
        if sql == str(skill_index._INVALID_SQL):
            return Result(params["index"] in self.invalid)
        self.ddl.append(sql)
        if "ATTACH PARTITION" in sql:
            self.attached.add(sql.rsplit(".", 1)[1].replace("_skill_entity_ts", ""))
        return Result(False)


class Result:
    def __init__(self, found):
        self.found = found

    def first(self):
        return (1,) if self.found else None


@pytest.fixture
def index_module(monkeypatch):
    def build(connection):
        monkeypatch.setattr(skill_index, "db", types.SimpleNamespace(
            engine=types.SimpleNamespace(connect=lambda: connection),
        ))
    return build


class TestSkillIndexTask:
    def test_builds_each_partition_concurrently_then_attaches_it(self, index_module):
        connection = FakeConnection(["usage_event_202609", "usage_event_202610"])
        index_module(connection)
        #
        result = skill_index.Method.usage_ensure_skill_index(None)
        #
        assert connection.isolation_level == "AUTOCOMMIT"
        assert connection.ddl[0] == skill_index.parent_index_statement("centry")
        assert "ON ONLY centry.usage_event" in connection.ddl[0]
        assert connection.ddl[1:] == [
            skill_index.partition_index_statement("centry", "usage_event_202609"),
            skill_index.attach_statement("centry", "usage_event_202609"),
            skill_index.partition_index_statement("centry", "usage_event_202610"),
            skill_index.attach_statement("centry", "usage_event_202610"),
        ]
        assert all("CONCURRENTLY" in sql for sql in connection.ddl[1::2])
        assert result == {"partitions": 2, "built": ["usage_event_202609", "usage_event_202610"]}

    def test_a_second_run_builds_nothing(self, index_module):
        connection = FakeConnection(["usage_event_202609", "usage_event_202610"])
        index_module(connection)
        skill_index.Method.usage_ensure_skill_index(None)
        connection.ddl.clear()
        #
        result = skill_index.Method.usage_ensure_skill_index(None)
        #
        assert connection.ddl == [skill_index.parent_index_statement("centry")]
        assert result["built"] == []

    def test_an_invalid_leftover_is_dropped_and_rebuilt(self, index_module):
        connection = FakeConnection(
            ["usage_event_202610"], invalid={"usage_event_202610_skill_entity_ts"},
        )
        index_module(connection)
        #
        skill_index.Method.usage_ensure_skill_index(None)
        #
        assert connection.ddl[1] == \
            "DROP INDEX CONCURRENTLY IF EXISTS centry.usage_event_202610_skill_entity_ts"
        assert connection.ddl[2] == skill_index.partition_index_statement("centry", "usage_event_202610")

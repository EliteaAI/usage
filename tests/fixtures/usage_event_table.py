"""An in-memory usage_event, so the real analytics SQL expressions run against sqlite."""
import importlib
import re
import sys
import types

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, create_engine, event,
)

METADATA = MetaData()

FAKE_TABLE = Table(
    "usage_event", METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
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
    Column("input_cost_nano_usd", Integer, default=0),
    Column("output_cost_nano_usd", Integer, default=0),
    Column("cache_read_cost_nano_usd", Integer, default=0),
    Column("cache_creation_cost_nano_usd", Integer, default=0),
    Column("duration_ms", Integer),
    Column("is_error", Boolean, default=False),
    Column("trigger_source", String),
    Column("meta", JSON),
)

FakeUsageEvent = types.SimpleNamespace(**{column.name: column for column in FAKE_TABLE.c})


def install_engine(monkeypatch, analytics):
    monkeypatch.setattr(analytics, "UsageEvent", FakeUsageEvent)
    monkeypatch.setattr(analytics, "HUMAN_ACTOR", FakeUsageEvent.user_id != analytics.SYSTEM_USER_ID)
    monkeypatch.setattr(analytics, "COST_SPLIT_COLUMNS", {
        "input_cost": FakeUsageEvent.input_cost_nano_usd,
        "output_cost": FakeUsageEvent.output_cost_nano_usd,
        "cache_read_cost": FakeUsageEvent.cache_read_cost_nano_usd,
        "cache_creation_cost": FakeUsageEvent.cache_creation_cost_nano_usd,
    })
    db_engine = create_engine("sqlite://")
    event.listen(db_engine, "connect", lambda connection, _record: connection.create_function(
        "regexp_replace", 3, lambda value, pattern, repl: None if value is None else re.sub(pattern, repl, value),
    ))
    METADATA.create_all(db_engine)

    def fetch_all(statement):
        with db_engine.connect() as connection:
            return list(connection.execute(statement).mappings())

    monkeypatch.setattr(analytics, "fetch_all", fetch_all)
    return db_engine


def drop_engine(db_engine):
    METADATA.drop_all(db_engine)


def insert_row(db_engine, **values):
    with db_engine.begin() as connection:
        connection.execute(FAKE_TABLE.insert().values(**values))


def trace_rows(db_engine, analytics, *conditions):
    with db_engine.connect() as connection:
        return list(connection.execute(analytics.run_trace_statement(conditions)).mappings())


def load_run_detail(monkeypatch):
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
    return importlib.import_module("usage.api.v2.analytics_run_detail")

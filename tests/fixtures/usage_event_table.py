"""An in-memory usage_event, so the real analytics SQL expressions run against sqlite."""
import importlib
import re
import sys
import types

from sqlalchemy import (
    JSON, Boolean, Cast, Column, Date, DateTime, Integer, MetaData, String, Table, create_engine, event,
)
from sqlalchemy.ext.compiler import compiles

METADATA = MetaData()

CURRENT_USER_ID = 11

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
    Column("cost_source", String),
    Column("duration_ms", Integer),
    Column("is_error", Boolean, default=False),
    Column("trigger_source", String),
    Column("budget_exempt", Boolean),
    Column("meta", JSON),
)

FakeUsageEvent = types.SimpleNamespace(**{column.name: column for column in FAKE_TABLE.c})


def _compile_event_started_at_sqlite(element, compiler, **kw):
    ts, duration_ms = (compiler.process(clause, **kw) for clause in element.clauses)
    # Same text shape as a stored DateTime, so min() and ORDER BY compare it with plain ts
    return (
        f"(strftime('%Y-%m-%d %H:%M:%f', {ts}, '-' || (coalesce({duration_ms}, 0) / 1000.0) || ' seconds')"
        " || '000')"
    )


def _compile_cast_date_sqlite(element, compiler, **kw):
    # CAST(ts AS DATE) on SQLite feeds a datetime object into the Date result_processor
    # (SQLAlchemy's DateTime type-processor fires first), so fromisoformat() gets a non-str
    # → TypeError.  SQLite's date() returns a plain 'YYYY-MM-DD' string that fromisoformat
    # handles correctly while still letting SQLAlchemy convert it to datetime.date.
    if isinstance(element.type, Date):
        return f"date({compiler.process(element.clause, **kw)})"
    return compiler.visit_cast(element, **kw)


_COMPILED_FOR_SQLITE = set()
_CAST_DATE_SQLITE_COMPILED = False


def install_engine(monkeypatch, analytics):
    global _CAST_DATE_SQLITE_COMPILED
    if analytics.EventStartedAt not in _COMPILED_FOR_SQLITE:
        compiles(analytics.EventStartedAt, "sqlite")(_compile_event_started_at_sqlite)
        _COMPILED_FOR_SQLITE.add(analytics.EventStartedAt)
    if not _CAST_DATE_SQLITE_COMPILED:
        compiles(Cast, "sqlite")(_compile_cast_date_sqlite)
        _CAST_DATE_SQLITE_COMPILED = True
    monkeypatch.setattr(analytics, "UsageEvent", FakeUsageEvent)
    monkeypatch.setattr(analytics, "HUMAN_ACTOR", FakeUsageEvent.user_id != analytics.SYSTEM_USER_ID)
    monkeypatch.setattr(analytics, "MANUAL_RUN", FakeUsageEvent.trigger_source.is_(None))
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
    return load_api_module(monkeypatch, "analytics_run_detail")


def load_api_module(monkeypatch, name):
    tools = sys.modules["tools"]
    monkeypatch.setattr(tools, "api_tools", types.SimpleNamespace(
        APIModeHandler=object, APIBase=object, with_modes=lambda params: params,
        endpoint_metrics=lambda func: func,
    ), raising=False)
    monkeypatch.setattr(tools, "auth", types.SimpleNamespace(
        decorators=types.SimpleNamespace(check_api=lambda *a, **k: (lambda func: func)),
        current_user=lambda: {"id": CURRENT_USER_ID},
    ), raising=False)
    monkeypatch.setattr(tools.config, "DEFAULT_MODE", "default", raising=False)
    monkeypatch.setattr(tools, "register_openapi", lambda *a, **k: (lambda func: func), raising=False)
    flask_stub = types.ModuleType("flask")
    flask_stub.request = types.SimpleNamespace(args={})
    monkeypatch.setitem(sys.modules, "flask", flask_stub)
    monkeypatch.delitem(sys.modules, f"usage.api.v2.{name}", raising=False)
    module = importlib.import_module(f"usage.api.v2.{name}")
    monkeypatch.setattr(module, "UsageEvent", FakeUsageEvent, raising=False)
    return module

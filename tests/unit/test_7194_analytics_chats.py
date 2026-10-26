"""Analytics Chats tab (#7194).

Covers the four chat analytics endpoints:
  - analytics_chats          (paginated conversation list)
  - analytics_chats_summary  (KPIs + participant breakdown)
  - analytics_chat_detail    (single-conversation breakdown)
  - analytics_chat_runs      (runs inside one conversation)

Key properties verified:
  - _TYPE_MAP: raw root_entity_type values (None, "application", "pipeline", "skill") map
    to the human-readable labels ("model", "agent", "pipeline", "skill") everywhere they appear
  - models field in the chats list is populated with {model_name, display_name} objects
  - chat_base_filters excludes non-chat rows (conversation_id IS NULL)
  - 404 returned when no data found for a specific conversation in analytics_chat_detail
  - Pagination and sort work in analytics_chat_runs
"""
import datetime
import json
import sys
import types
import uuid

import pytest

from fixtures.usage_event_table import FakeUsageEvent, drop_engine, insert_row, install_engine, load_api_module
from usage.methods import _analytics
from usage.methods import analytics_chats as ac


# ---------------------------------------------------------------------------
# SQLite compat for array_agg.
#
# analytics_chats.py uses func.array_agg(distinct(col)) which PostgreSQL handles natively.
# For tests we register a Python aggregate class so SQLite understands the call and returns
# a JSON-encoded list. The test engine fixture then parses it back to a Python list.
# ---------------------------------------------------------------------------

class _ArrayAggAggregate:
    """SQLite custom aggregate: collects non-NULL values into a JSON-encoded list."""

    def __init__(self):
        self._seen = []

    def step(self, value):
        if value not in self._seen:
            self._seen.append(value)

    def finalize(self):
        return json.dumps(self._seen)


# SQLAlchemy compiles func.array_agg(distinct(col)) as "array_agg(DISTINCT col)".
# SQLite does not support DISTINCT inside user aggregates. Register a compile override
# that strips DISTINCT so the SQL becomes plain "array_agg(col)"; dedup happens in Python.
from sqlalchemy.ext.compiler import compiles  # noqa: E402  pylint: disable=C0413
from sqlalchemy.sql.functions import GenericFunction  # noqa: E402  pylint: disable=C0413


class _ArrayAggFunc(GenericFunction):
    """Override for func.array_agg so SQLAlchemy can compile it for SQLite."""
    name = "array_agg"
    inherit_cache = True


@compiles(_ArrayAggFunc, "sqlite")
def _compile_array_agg_sqlite(element, compiler, **kw):
    """Emit plain array_agg(col) without DISTINCT (dedup is handled in Python)."""
    from sqlalchemy.sql.elements import UnaryExpression  # pylint: disable=C0415
    from sqlalchemy.sql.operators import distinct_op  # pylint: disable=C0415

    clauses = list(element.clauses)
    inner = clauses[0] if clauses else None
    if isinstance(inner, UnaryExpression) and getattr(inner, "modifier", None) is distinct_op:
        inner = inner.element
    col_sql = compiler.process(inner, **kw) if inner is not None else "NULL"
    return f"array_agg({col_sql})"

PROJECT_ID = 2
TS = datetime.datetime(2026, 10, 8, 12, 0, 0)
WINDOW = (TS - datetime.timedelta(days=2), TS + datetime.timedelta(days=2))
CONV_1 = "00000000-0000-0000-0000-000000000001"
CONV_2 = "00000000-0000-0000-0000-000000000002"
RUN_1 = "aaaaaaaa-0000-0000-0000-000000000001"
RUN_2 = "aaaaaaaa-0000-0000-0000-000000000002"
RUN_3 = "aaaaaaaa-0000-0000-0000-000000000003"
API_MODULES = (
    "analytics_chats",
    "analytics_chats_summary",
    "analytics_chat_detail",
    "analytics_chat_runs",
)


def _register_array_agg_on_connection(raw_dbapi_conn):
    """Register the Python array_agg aggregate on a raw SQLite DBAPI connection."""
    try:
        raw_dbapi_conn.create_aggregate("array_agg", 1, _ArrayAggAggregate)
    except Exception:  # already registered on this connection — safe to ignore
        pass


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    monkeypatch.setattr(_analytics, "model_display_names", lambda project_id: {
        "gpt-4": {"shared": "GPT-4 Turbo"},
        "claude-3": {"shared": "Claude 3 Opus"},
    })
    monkeypatch.setattr(_analytics, "label_users", lambda user_ids, stored=None: {
        11: "alice@example.com", 12: "bob@example.com",
    })
    monkeypatch.setattr(_analytics, "application_meta", lambda project_id, refs=(): {})
    monkeypatch.setattr(_analytics, "parse_date_range", lambda args, run_scope=None: WINDOW)

    # Register array_agg on every connection (existing and future). With sqlite://,
    # SQLAlchemy uses a single pooled connection; we touch it via an event that fires on
    # checkout, guaranteeing registration before any user query runs.
    from sqlalchemy import event as sa_event  # pylint: disable=C0415

    def _on_checkout(dbapi_conn, _conn_record, _conn_proxy):
        _register_array_agg_on_connection(dbapi_conn)

    sa_event.listen(db_engine, "checkout", _on_checkout)

    # analytics_chats.py imports fetch_all / fetch_one / UsageEvent by name at import time,
    # so the _analytics patches from install_engine don't reach them. Re-patch on ac itself.
    monkeypatch.setattr(ac, "UsageEvent", FakeUsageEvent)

    # Wrap the already-patched fetch_all to parse participant_types from the JSON-encoded
    # list that our custom array_agg aggregate returns. Also forward to ac.
    _sqlite_fetch_all = _analytics.fetch_all

    def _fetch_all_compat(statement):
        rows = _sqlite_fetch_all(statement)
        result = []
        for row in rows:
            row = dict(row)
            if "participant_types" in row and isinstance(row["participant_types"], str):
                row["participant_types"] = json.loads(row["participant_types"])
            result.append(row)
        return result

    monkeypatch.setattr(_analytics, "fetch_all", _fetch_all_compat)
    monkeypatch.setattr(ac, "fetch_all", _fetch_all_compat)
    monkeypatch.setattr(ac, "fetch_one", _analytics.fetch_one)

    yield db_engine
    drop_engine(db_engine)


@pytest.fixture
def core(monkeypatch):
    """Fake elitea_core RPC that answers conversation metadata requests.

    participant_types is intentionally absent from the metadata so that tests for
    _TYPE_MAP in analytics_chats exercise the SQL-derived fallback path in resolve_chat_meta.
    """
    state = {
        "conversations": {
            CONV_1: {"id": 101, "name": "Code review chat", "is_private": False, "author_id": 11},
            CONV_2: {"id": 102, "name": "Agent chat", "is_private": False, "author_id": 12},
        },
        "non_chat_uuids": [],
        "calls": [],
    }

    class Rpc:
        def timeout(self, _seconds):
            return self

        def elitea_core_usage_entity_meta(self, project_id, **kwargs):
            state["calls"].append(kwargs)
            if "conversation_uuids" in kwargs:
                convs = kwargs["conversation_uuids"]
                return {
                    "conversations": [
                        {
                            "uuid": u,
                            "id": state["conversations"][u]["id"],
                            "name": state["conversations"][u]["name"],
                            "is_private": state["conversations"][u]["is_private"],
                            "author_id": state["conversations"][u]["author_id"],
                            # participant_types intentionally absent: tests use the SQL-derived fallback
                        }
                        for u in convs
                        if u in state["conversations"]
                    ],
                }
            if kwargs.get("fetch_non_chat_uuids"):
                return {"non_chat_uuids": state["non_chat_uuids"]}
            if kwargs.get("conversation_name_search"):
                search = kwargs["conversation_name_search"].lower()
                return {
                    "conversations": [
                        {"uuid": u, **meta}
                        for u, meta in state["conversations"].items()
                        if search in meta["name"].lower()
                    ]
                }
            return {}

    # Clear the excluded-uuid cache so each test starts fresh
    ac._EXCLUDED_UUID_CACHE.clear()

    monkeypatch.setattr(sys.modules["tools"], "rpc_tools", types.SimpleNamespace(
        RpcMixin=lambda: types.SimpleNamespace(rpc=Rpc()),
    ), raising=False)
    yield state


@pytest.fixture
def api(monkeypatch, engine, core):
    modules = types.SimpleNamespace(**{name: load_api_module(monkeypatch, name) for name in API_MODULES})
    # Make analytics_chat_detail and analytics_chat_runs use the same patched analytics_chats module
    for name in API_MODULES:
        mod = getattr(modules, name)
        if hasattr(mod, "ac"):
            monkeypatch.setattr(mod, "ac", ac)
    yield modules
    for name in API_MODULES:
        sys.modules.pop(f"usage.api.v2.{name}", None)


def insert(db_engine, run_id=RUN_1, conversation_id=CONV_1, seconds=0, **values):
    """Insert a chat usage event row with sensible defaults."""
    row = {
        "ts": TS + datetime.timedelta(seconds=seconds),
        "project_id": PROJECT_ID,
        "user_id": 11,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "event_type": "llm",
        "model_name": "gpt-4",
        "root_entity_type": None,  # direct model call by default
        "root_entity_id": None,
        "billable_input_tokens": 100,
        "output_tokens": 10,
        "cost_nano_usd": 1_000,
        "is_error": False,
    }
    row.update(values)
    insert_row(db_engine, **row)


def chats_list(api, monkeypatch, **args):
    monkeypatch.setattr(api.analytics_chats, "request", types.SimpleNamespace(args={**args}))
    body, status = api.analytics_chats.PromptLibAPI().get(PROJECT_ID)
    assert status == 200, body
    return body


def summary(api, monkeypatch, **args):
    monkeypatch.setattr(api.analytics_chats_summary, "request", types.SimpleNamespace(args={**args}))
    body, status = api.analytics_chats_summary.PromptLibAPI().get(PROJECT_ID)
    assert status == 200, body
    return body


def detail(api, monkeypatch, conversation_uuid=CONV_1, **args):
    monkeypatch.setattr(api.analytics_chat_detail, "request", types.SimpleNamespace(args={
        "conversation_uuid": conversation_uuid, **args,
    }))
    return api.analytics_chat_detail.PromptLibAPI().get(PROJECT_ID)


def runs(api, monkeypatch, conversation_uuid=CONV_1, **args):
    monkeypatch.setattr(api.analytics_chat_runs, "request", types.SimpleNamespace(args={
        "conversation_uuid": conversation_uuid, **args,
    }))
    body, status = api.analytics_chat_runs.PromptLibAPI().get(PROJECT_ID)
    assert status == 200, body
    return body


class TestChatBaseFilters:
    def test_rows_without_conversation_id_are_excluded(self, api, engine, monkeypatch):
        insert(engine, conversation_id=None)  # no conversation_id
        insert(engine, run_id=RUN_2)          # has conversation_id

        body = chats_list(api, monkeypatch)

        assert body["total"] == 1

    def test_excluded_non_chat_uuids_are_filtered(self, api, engine, monkeypatch, core):
        insert(engine, run_id=RUN_1, conversation_id=CONV_1)
        insert(engine, run_id=RUN_2, conversation_id=CONV_2)
        core["non_chat_uuids"] = [CONV_2]
        # Clear cache so new exclusion list is fetched
        ac._EXCLUDED_UUID_CACHE.clear()

        body = chats_list(api, monkeypatch)

        uuids = [r["conversation_uuid"] for r in body["rows"]]
        assert CONV_1 in uuids
        assert CONV_2 not in uuids


def _make_raw_row(conversation_uuid, participant_types, run_id=RUN_1, ts=None):
    """Build a minimal raw row as returned by fetch_all before _TYPE_MAP is applied."""
    ts = ts or TS
    return {
        "conversation_uuid": conversation_uuid,
        "responses": 1,
        "users": 1,
        "errors": 0,
        "last_activity": ts,
        "avg_duration_ms": None,
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 110,
        "cost_nano": 1_000,
        "participant_types": participant_types,
        "input_cost_nano": 0,
        "output_cost_nano": 0,
        "cache_read_cost_nano": 0,
        "cache_creation_cost_nano": 0,
    }


class TestTypeMapInChatsList:
    """participant_types in the chats list apply _TYPE_MAP to produce human-readable labels.

    SQLite lacks array_agg, so these tests patch fetch_all to return pre-built raw rows
    that mirror what PostgreSQL would return, then verify the _TYPE_MAP translation.
    """

    def _patch_fetch_all(self, monkeypatch, raw_rows):
        """Make _analytics.fetch_all return raw_rows for the main data query (not the COUNT)."""
        call_count = {"n": 0}

        def fake_fetch_all(statement):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call is the COUNT(*) subquery — return total
                return [{"total": len(raw_rows)}]
            # Second call is the paginated data query
            return raw_rows

        monkeypatch.setattr(_analytics, "fetch_all", fake_fetch_all)

    def test_none_root_entity_type_maps_to_model(self, api, engine, monkeypatch):
        self._patch_fetch_all(monkeypatch, [_make_raw_row(CONV_1, [None])])

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]

        assert row["participant_types"] == [{"type": "model"}]

    def test_application_root_entity_type_maps_to_agent(self, api, engine, monkeypatch):
        self._patch_fetch_all(monkeypatch, [_make_raw_row(CONV_1, ["application"])])

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]

        assert row["participant_types"] == [{"type": "agent"}]

    def test_pipeline_root_entity_type_stays_pipeline(self, api, engine, monkeypatch):
        self._patch_fetch_all(monkeypatch, [_make_raw_row(CONV_1, ["pipeline"])])

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]

        assert row["participant_types"] == [{"type": "pipeline"}]

    def test_skill_root_entity_type_stays_skill(self, api, engine, monkeypatch):
        self._patch_fetch_all(monkeypatch, [_make_raw_row(CONV_1, ["skill"])])

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]

        assert row["participant_types"] == [{"type": "skill"}]

    def test_mixed_types_deduplicated(self, api, engine, monkeypatch):
        self._patch_fetch_all(monkeypatch, [_make_raw_row(CONV_1, [None, "application", "application"])])

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]
        types_seen = {p["type"] for p in row["participant_types"]}

        assert types_seen == {"model", "agent"}


class TestModelsInChatsList:
    """models field is populated with {model_name, display_name} objects."""

    def test_models_field_contains_display_name(self, api, engine, monkeypatch):
        insert(engine, model_name="gpt-4")

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]

        assert isinstance(row["models"], list)
        assert len(row["models"]) == 1
        model = row["models"][0]
        assert model["model_name"] == "gpt-4"
        assert model["display_name"] == "GPT-4 Turbo"

    def test_multiple_models_all_appear(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, model_name="gpt-4")
        insert(engine, run_id=RUN_2, model_name="claude-3", seconds=1)

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]
        model_names = {m["model_name"] for m in row["models"]}

        assert model_names == {"gpt-4", "claude-3"}


class TestChatsMeta:
    """resolve_chat_meta enriches rows with name, is_deleted, owner_id."""

    def test_chat_name_comes_from_core(self, api, engine, monkeypatch):
        insert(engine)

        body = chats_list(api, monkeypatch)
        row = next(r for r in body["rows"] if r["conversation_uuid"] == CONV_1)

        assert row["chat_name"] == "Code review chat"

    def test_deleted_conversation_has_is_deleted_true(self, api, engine, monkeypatch, core):
        insert(engine)
        # Remove from core's known conversations so it looks deleted
        del core["conversations"][CONV_1]

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]

        assert row["is_deleted"] is True

    def test_known_conversation_has_is_deleted_false(self, api, engine, monkeypatch):
        insert(engine)

        body = chats_list(api, monkeypatch)
        row = body["rows"][0]

        assert row["is_deleted"] is False


class TestSummaryKpis:
    def test_active_chats_counts_distinct_conversations(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, conversation_id=CONV_1)
        insert(engine, run_id=RUN_2, conversation_id=CONV_1, seconds=1)
        insert(engine, run_id=RUN_3, conversation_id=CONV_2, seconds=2)

        body = summary(api, monkeypatch)

        assert body["kpis"]["active_chats"] == 2

    def test_responses_counts_distinct_runs(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1)
        insert(engine, run_id=RUN_1, seconds=1)  # same run
        insert(engine, run_id=RUN_2, seconds=2)

        body = summary(api, monkeypatch)

        assert body["kpis"]["responses"] == 2

    def test_total_tokens_sum(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, billable_input_tokens=100, output_tokens=50)
        insert(engine, run_id=RUN_2, billable_input_tokens=200, output_tokens=100, seconds=1)

        body = summary(api, monkeypatch)

        # billable_input + output for both rows
        assert body["kpis"]["total_tokens"] == 450

    def test_errors_count_only_error_runs(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1)
        insert(engine, run_id=RUN_2, is_error=True, seconds=1)

        body = summary(api, monkeypatch)

        assert body["kpis"]["errors"] == 1


class TestSummaryParticipantBreakdown:
    """by_participant_type uses human-readable type labels."""

    def test_none_entity_type_appears_as_model(self, api, engine, monkeypatch):
        insert(engine, root_entity_type=None)

        body = summary(api, monkeypatch)
        types_found = [row["type"] for row in body["by_participant_type"]]

        assert "model" in types_found
        assert "application" not in types_found

    def test_application_entity_type_appears_as_agent(self, api, engine, monkeypatch):
        insert(engine, root_entity_type="application")

        body = summary(api, monkeypatch)
        types_found = [row["type"] for row in body["by_participant_type"]]

        assert "agent" in types_found
        assert "application" not in types_found

    def test_responses_in_breakdown_match_kpi(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, root_entity_type=None)
        insert(engine, run_id=RUN_2, root_entity_type="application", seconds=1)

        body = summary(api, monkeypatch)
        total_from_breakdown = sum(row["responses"] for row in body["by_participant_type"])

        assert total_from_breakdown == body["kpis"]["responses"]


class TestChatDetail:
    def test_404_when_no_events_for_conversation(self, api, engine, monkeypatch):
        body, status = detail(api, monkeypatch, conversation_uuid=CONV_1)

        assert status == 404

    def test_400_on_invalid_uuid(self, api, engine, monkeypatch):
        body, status = detail(api, monkeypatch, conversation_uuid="not-a-uuid")

        assert status == 400

    def test_kpis_returned_when_data_present(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, billable_input_tokens=100, output_tokens=50, cost_nano_usd=1_500)
        insert(engine, run_id=RUN_2, billable_input_tokens=200, seconds=1)

        body, status = detail(api, monkeypatch)

        assert status == 200
        assert body["kpis"]["responses"] == 2
        assert body["kpis"]["total_tokens"] > 0

    def test_participant_type_application_maps_to_agent(self, api, engine, monkeypatch):
        insert(engine, root_entity_type="application", root_entity_id=55)

        body, status = detail(api, monkeypatch)

        assert status == 200
        participants = body["by_participant"]
        agent_row = next((p for p in participants if p["participant_type"] == "agent"), None)
        assert agent_row is not None, f"expected 'agent', got: {[p['participant_type'] for p in participants]}"

    def test_participant_type_none_maps_to_model(self, api, engine, monkeypatch):
        insert(engine, root_entity_type=None)

        body, status = detail(api, monkeypatch)

        assert status == 200
        participants = body["by_participant"]
        model_row = next((p for p in participants if p["participant_type"] == "model"), None)
        assert model_row is not None, f"expected 'model', got: {[p['participant_type'] for p in participants]}"

    def test_raw_application_value_never_in_participants(self, api, engine, monkeypatch):
        insert(engine, root_entity_type="application", root_entity_id=55)

        body, status = detail(api, monkeypatch)

        assert status == 200
        raw_types = [p["participant_type"] for p in body["by_participant"]]
        assert "application" not in raw_types

    def test_models_breakdown_uses_display_name(self, api, engine, monkeypatch):
        insert(engine, model_name="gpt-4")

        body, status = detail(api, monkeypatch)

        assert status == 200
        model_row = next((m for m in body["by_model"] if m["model_name"] == "gpt-4"), None)
        assert model_row is not None
        assert model_row["display_name"] == "GPT-4 Turbo"

    def test_first_and_last_activity_are_present(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, seconds=0)
        insert(engine, run_id=RUN_2, seconds=60)

        body, status = detail(api, monkeypatch)

        assert status == 200
        assert body["kpis"]["first_activity"] is not None
        assert body["kpis"]["last_activity"] is not None
        # last_activity must be after first_activity
        assert body["kpis"]["last_activity"] > body["kpis"]["first_activity"]


class TestChatRuns:
    def test_400_when_conversation_uuid_missing(self, api, engine, monkeypatch):
        monkeypatch.setattr(api.analytics_chat_runs, "request", types.SimpleNamespace(args={}))
        body, status = api.analytics_chat_runs.PromptLibAPI().get(PROJECT_ID)

        assert status == 400

    def test_400_on_invalid_uuid(self, api, engine, monkeypatch):
        monkeypatch.setattr(api.analytics_chat_runs, "request", types.SimpleNamespace(args={
            "conversation_uuid": "bad-uuid",
        }))
        body, status = api.analytics_chat_runs.PromptLibAPI().get(PROJECT_ID)

        assert status == 400

    def test_participant_type_application_maps_to_agent(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, root_entity_type="application", root_entity_id=55)

        body = runs(api, monkeypatch)
        row = body["rows"][0]

        assert row["participant_type"] == "agent"
        assert row["participant_type"] != "application"

    def test_participant_type_none_maps_to_model(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, root_entity_type=None)

        body = runs(api, monkeypatch)
        row = body["rows"][0]

        assert row["participant_type"] == "model"

    def test_participant_type_pipeline_unchanged(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, root_entity_type="pipeline")

        body = runs(api, monkeypatch)

        assert body["rows"][0]["participant_type"] == "pipeline"

    def test_participant_type_skill_unchanged(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, root_entity_type="skill")

        body = runs(api, monkeypatch)

        assert body["rows"][0]["participant_type"] == "skill"

    def test_models_per_run_are_objects_not_strings(self, api, engine, monkeypatch):
        """models field must be [{model_name, display_name}], not plain strings."""
        insert(engine, run_id=RUN_1, model_name="gpt-4")

        body = runs(api, monkeypatch)
        row = body["rows"][0]

        assert isinstance(row["models"], list)
        for m in row["models"]:
            assert "model_name" in m
            assert "display_name" in m

    def test_models_display_name_resolved(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, model_name="gpt-4")

        body = runs(api, monkeypatch)
        model = body["rows"][0]["models"][0]

        assert model["model_name"] == "gpt-4"
        assert model["display_name"] == "GPT-4 Turbo"

    def test_pagination_total_equals_all_runs(self, api, engine, monkeypatch):
        for i in range(5):
            insert(engine, run_id=str(uuid.uuid4()), seconds=i * 10)

        body = runs(api, monkeypatch, limit="2", offset="0")

        assert body["total"] == 5
        assert len(body["rows"]) == 2

    def test_pagination_offset_returns_remaining_rows(self, api, engine, monkeypatch):
        for i in range(5):
            insert(engine, run_id=str(uuid.uuid4()), seconds=i * 10)

        body = runs(api, monkeypatch, limit="3", offset="3")

        assert len(body["rows"]) == 2

    def test_only_runs_from_requested_conversation(self, api, engine, monkeypatch):
        insert(engine, run_id=RUN_1, conversation_id=CONV_1)
        insert(engine, run_id=RUN_2, conversation_id=CONV_2, seconds=1)

        body = runs(api, monkeypatch, conversation_uuid=CONV_1)

        assert all(r["run_id"] == RUN_1 for r in body["rows"])
        assert len(body["rows"]) == 1

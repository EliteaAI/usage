"""Date-range contract and leaderboard determinism for Usage analytics (#6879).

A date bound that is present but unusable used to become "absent" and then the seven-day default,
with nothing in the response to say so. The Overview now rejects it, and reports the window it
really used. The Top 5 leaderboard breaks ties by user_id so equal scores rank the same every time.
"""
import datetime
import sys
import types

import pytest
from sqlalchemy import Date, func

from fixtures.usage_event_table import FakeUsageEvent, drop_engine, insert_row, install_engine, load_api_module
from usage.methods import _analytics

PROJECT_ID = 2
TS = datetime.datetime(2026, 10, 8, 12, 0, 0)
WINDOW = (TS - datetime.timedelta(days=2), TS + datetime.timedelta(days=2))
OUTSIDE = TS - datetime.timedelta(days=30)
# The fixture below replaces label_users; the deleted-account tests need the real fallback
REAL_LABEL_USERS = _analytics.label_users


class TestDateRangeError:
    def test_no_bounds_is_accepted_and_defaulted_later(self):
        assert _analytics.date_range_error({}) is None

    def test_one_valid_bound_is_accepted(self):
        assert _analytics.date_range_error({"date_to": "2026-10-08T00:00:00Z"}) is None
        assert _analytics.date_range_error({"date_from": "2026-10-08"}) is None

    @pytest.mark.parametrize("key", ["date_from", "date_to"])
    def test_an_unparseable_bound_is_rejected_not_defaulted(self, key):
        body, status = _analytics.date_range_error({key: "not-a-date"})
        assert status == 400
        assert key in body["error"]

    def test_from_after_to_is_rejected(self):
        body, status = _analytics.date_range_error({
            "date_from": "2026-10-09T00:00:00Z", "date_to": "2026-10-08T00:00:00Z",
        })
        assert status == 400
        assert "date_from" in body["error"]

    def test_a_naive_and_an_aware_bound_compare_without_raising(self):
        assert _analytics.date_range_error({
            "date_from": "2026-10-08", "date_to": "2026-10-09T00:00:00Z",
        }) is None


class TestDateRangeMeta:
    def test_both_bounds_given_reports_nothing_defaulted(self):
        args = {"date_from": "2026-10-01T00:00:00Z", "date_to": "2026-10-08T00:00:00Z"}
        dt_from, dt_to = _analytics.parse_date_range(args)
        meta = _analytics.date_range_meta(args, dt_from, dt_to)
        assert meta["defaulted"] == []
        assert meta["clamped"] is False
        assert meta["timezone"] == "UTC"
        assert meta["date_from"] == "2026-10-01T00:00:00+00:00"
        assert meta["date_to"] == "2026-10-08T00:00:00+00:00"

    def test_a_single_bound_names_the_one_the_backend_filled_in(self):
        args = {"date_to": "2026-10-08T00:00:00Z"}
        dt_from, dt_to = _analytics.parse_date_range(args)
        meta = _analytics.date_range_meta(args, dt_from, dt_to)
        assert meta["defaulted"] == ["date_from"]
        assert meta["date_from"] == "2026-10-01T00:00:00+00:00"

    def test_no_bounds_names_both(self):
        dt_from, dt_to = _analytics.parse_date_range({})
        assert _analytics.date_range_meta({}, dt_from, dt_to)["defaulted"] == ["date_from", "date_to"]

    def test_a_span_over_the_maximum_is_reported_as_clamped(self):
        args = {"date_from": "2024-01-01T00:00:00Z", "date_to": "2026-01-01T00:00:00Z"}
        dt_from, dt_to = _analytics.parse_date_range(args)
        meta = _analytics.date_range_meta(args, dt_from, dt_to)
        assert meta["clamped"] is True
        assert (dt_to - dt_from).days == _analytics.MAX_DATE_RANGE_DAYS

    def test_a_run_scoped_query_with_no_bounds_is_not_reported_as_defaulted(self):
        scope = _analytics.RunScope(run_id="r", dt_from=WINDOW[0], dt_to=WINDOW[1])
        dt_from, dt_to = _analytics.parse_date_range({}, run_scope=scope)
        assert _analytics.date_range_meta({}, dt_from, dt_to, run_scope=scope)["defaulted"] == []


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    monkeypatch.setattr(_analytics, "label_users", lambda user_ids, stored=None: {
        uid: f"user{uid}@example.com" for uid in user_ids if uid is not None
    })
    monkeypatch.setattr(_analytics, "outsider_admin_ids", lambda project_id, conditions: set())
    monkeypatch.setattr(_analytics, "model_display_names", lambda project_id: {})
    monkeypatch.setattr(_analytics, "day_expr", lambda: func.date(FakeUsageEvent.ts, type_=Date))
    yield db_engine
    drop_engine(db_engine)


@pytest.fixture
def api(monkeypatch, engine):
    modules = types.SimpleNamespace(
        analytics=load_api_module(monkeypatch, "analytics"),
        analytics_costs=load_api_module(monkeypatch, "analytics_costs"),
    )
    yield modules
    for name in ("analytics", "analytics_costs"):
        sys.modules.pop(f"usage.api.v2.{name}", None)


def call(engine, user_id, seconds=0, **values):
    row = {
        "ts": TS + datetime.timedelta(seconds=seconds), "project_id": PROJECT_ID, "user_id": user_id,
        "run_id": f"run-{user_id}-{seconds}", "event_type": "llm", "model_name": "gpt-x", "is_error": False,
    }
    row.update(values)
    insert_row(engine, **row)


def leaderboard(api):
    conditions = _analytics.base_filters(PROJECT_ID, *WINDOW)
    return api.analytics.PromptLibAPI._top_ai_users(PROJECT_ID, conditions)


class TestLeaderboard:
    def test_equal_scores_are_ordered_by_user_id_whatever_the_insert_order(self, engine, api):
        for user_id in (31, 12, 20):
            call(engine, user_id, seconds=user_id)
            call(engine, user_id, seconds=user_id + 100)
        assert [u["user_id"] for u in leaderboard(api)] == [12, 20, 31]

    def test_a_higher_score_still_outranks_a_lower_user_id(self, engine, api):
        call(engine, 5)
        call(engine, 40, seconds=1)
        call(engine, 40, seconds=2)
        assert [u["user_id"] for u in leaderboard(api)] == [40, 5]

    def test_a_user_active_only_outside_the_range_does_not_appear(self, engine, api):
        call(engine, 7)
        call(engine, 8, ts=OUTSIDE)
        assert [u["user_id"] for u in leaderboard(api)] == [7]

    def test_no_activity_in_the_range_is_an_empty_list(self, engine, api):
        call(engine, 8, ts=OUTSIDE)
        assert leaderboard(api) == []

    def test_a_deleted_account_keeps_its_in_range_activity_and_is_flagged(self, engine, api, monkeypatch):
        # The directory answered and no longer knows user 9: the history ranks, marked deleted
        monkeypatch.setattr(_analytics, "label_users", REAL_LABEL_USERS)
        monkeypatch.setattr(_analytics, "resolve_directory", lambda ids: ({10: "here@example.com"}, {9}))
        call(engine, 9, user_email="gone@example.com")
        call(engine, 9, seconds=1, user_email="gone@example.com")
        call(engine, 10, seconds=2, user_email="here@example.com")

        rows = leaderboard(api)

        assert [(u["user_id"], u["ai_events"], u["user_email"], u["user_deleted"]) for u in rows] == [
            (9, 2, None, True), (10, 1, "here@example.com", False),
        ]

    def test_a_deleted_accounts_stored_email_is_not_shown(self, engine, api, monkeypatch):
        monkeypatch.setattr(_analytics, "label_users", REAL_LABEL_USERS)
        monkeypatch.setattr(_analytics, "resolve_directory", lambda ids: ({}, {9}))
        call(engine, 9, user_email="gone@example.com")

        assert "gone@example.com" not in str(leaderboard(api))

    def test_an_unresolved_user_is_not_flagged_deleted_when_the_directory_could_not_answer(
            self, engine, api, monkeypatch):
        monkeypatch.setattr(_analytics, "label_users", REAL_LABEL_USERS)
        monkeypatch.setattr(_analytics, "resolve_directory", lambda ids: ({}, set()))
        call(engine, 9, user_email="kept@example.com")

        rows = leaderboard(api)

        assert [(u["user_email"], u["user_deleted"]) for u in rows] == [("kept@example.com", False)]

    def test_only_five_are_returned(self, engine, api):
        for user_id in range(1, 9):
            call(engine, user_id, seconds=user_id)
        assert [u["user_id"] for u in leaderboard(api)] == [1, 2, 3, 4, 5]

    def test_llm_and_tool_rows_are_each_counted_once(self, engine, api):
        call(engine, 3)
        call(engine, 3, seconds=1, event_type="tool", tool_name="search", model_name=None)
        row = leaderboard(api)[0]
        assert (row["ai_events"], row["llm_calls"], row["tool_runs"]) == (2, 1, 1)


class TestLabelUsers:
    def test_directory_wins_then_stored_email_then_nothing(self, monkeypatch):
        monkeypatch.setattr(_analytics, "resolve_directory", lambda ids: ({1: "live@example.com"}, set()))
        labels = _analytics.label_users([1, 2, 3], {1: "old@example.com", 2: "stored@example.com"})
        assert labels == {1: "live@example.com", 2: "stored@example.com", 3: None}

    def test_a_confirmed_deleted_user_has_no_email_whatever_the_row_stored(self, monkeypatch):
        monkeypatch.setattr(_analytics, "resolve_directory", lambda ids: ({}, {7}))
        labels = _analytics.label_users([7], {7: "gone@example.com"})
        assert labels == {7: None}
        assert labels.deleted == {7}
        assert _analytics.user_fields(labels, 7) == {"user_email": None, "user_deleted": True}

    def test_user_fields_for_a_live_user_a_missing_id_and_a_plain_dict(self):
        labels = _analytics.UserLabels({1: "a@example.com"})
        assert _analytics.user_fields(labels, 1) == {"user_email": "a@example.com", "user_deleted": False}
        assert _analytics.user_fields(labels, None) == {"user_email": None, "user_deleted": False}
        assert _analytics.user_fields({1: "a@example.com"}, 1) == {"user_email": "a@example.com", "user_deleted": False}

    def test_the_system_actor_is_never_called_deleted(self, monkeypatch):
        monkeypatch.setattr(_analytics, "_lookup_users", lambda ids: ([], True))
        assert _analytics.resolve_directory([0, None]) == ({}, set())
        assert _analytics.label_users([0], {0: None}) == {0: None}
        assert _analytics.label_users([0], {0: None}).deleted == frozenset()


class TestResolveDirectory:
    """Real resolve_directory over a stubbed directory: deleted only when the lookup answered."""

    @staticmethod
    def directory(monkeypatch, *, users=(), list_raises=False, get_user_ids=()):
        auth = types.SimpleNamespace()

        def list_users(user_ids):
            if list_raises:
                raise RuntimeError("directory down")
            return [u for u in users if u["id"] in user_ids]

        auth.list_users = list_users
        auth.get_user = lambda user_id: next((u for u in users if u["id"] == user_id and user_id in get_user_ids), None)
        tools = types.ModuleType("tools")
        tools.auth = auth
        monkeypatch.setitem(sys.modules, "tools", tools)

    def test_an_id_missing_from_an_answered_lookup_is_deleted(self, monkeypatch):
        self.directory(monkeypatch, users=[{"id": 1, "email": "a@example.com"}])
        assert _analytics.resolve_directory([1, 2]) == ({1: "a@example.com"}, {2})

    def test_a_lookup_outage_never_marks_anyone_deleted(self, monkeypatch):
        users = [{"id": 1, "email": "a@example.com"}]
        self.directory(monkeypatch, users=users, list_raises=True, get_user_ids=(1,))
        resolved, deleted = _analytics.resolve_directory([1, 2])
        assert resolved == {1: "a@example.com"}
        assert deleted == set()


SYSTEM_USER = 0
WINDOW_FROM, WINDOW_TO = WINDOW


def mixed_population(engine):
    """chat + embedding models, a tool, a project-own model, the system account, an automated run,
    and rows outside the window. Every number below is derived from this one fixture."""
    call(engine, 1, input_tokens=100, billable_input_tokens=100, output_tokens=20, cost_nano_usd=4_000)
    call(engine, 1, seconds=1, model_name="text-embedding-3-small", input_tokens=50,
         billable_input_tokens=50, output_tokens=0, cost_nano_usd=100)
    call(engine, 2, seconds=2, input_tokens=10, billable_input_tokens=10, output_tokens=5, cost_nano_usd=700,
         budget_exempt=True, model_name="own-gpt")
    call(engine, SYSTEM_USER, seconds=3, model_name="text-embedding-3-small", input_tokens=30,
         billable_input_tokens=30, cost_nano_usd=60, trigger_source="index")
    call(engine, 3, seconds=4, input_tokens=7, billable_input_tokens=7, cost_nano_usd=50, trigger_source="scheduled")
    call(engine, 1, seconds=5, event_type="tool", tool_name="search", model_name=None)
    call(engine, 1, ts=OUTSIDE, input_tokens=999, billable_input_tokens=999, cost_nano_usd=999)


def overview(api):
    return api.analytics.PromptLibAPI


class TestModelPopulation:
    def test_the_overview_model_totals_include_embedding_system_and_own_model_calls(self, engine, api):
        mixed_population(engine)
        model_conditions = _analytics.model_call_filters(PROJECT_ID, *WINDOW)
        people_conditions = _analytics.base_filters(PROJECT_ID, *WINDOW)

        kpis = overview(api)._kpis(PROJECT_ID, people_conditions, None, model_conditions)

        assert kpis["llm_calls"] == 5          # 1 chat, 2 embedding, 1 own, 1 scheduled
        assert kpis["total_tokens"] == 120 + 50 + 15 + 30 + 7
        assert kpis["total_llm_cost"] == _analytics.cost_usd(4_000 + 100 + 700 + 60 + 50)

    def test_the_overview_and_the_costs_tab_see_the_same_calls_tokens_and_cost(self, engine, api):
        mixed_population(engine)
        costs_api = api.analytics_costs.PromptLibAPI
        model_conditions = _analytics.model_call_filters(PROJECT_ID, *WINDOW)

        kpis = overview(api)._kpis(
            PROJECT_ID, _analytics.base_filters(PROJECT_ID, *WINDOW), None, model_conditions,
        )
        by_model = costs_api._by_model(PROJECT_ID, model_conditions)
        cost_kpis = costs_api._kpis(model_conditions)

        assert sum(m["calls"] for m in by_model) == kpis["llm_calls"]
        assert sum(m["total_tokens"] for m in by_model) == kpis["total_tokens"] == cost_kpis["total_tokens"]
        assert cost_kpis["total_cost"] == kpis["total_llm_cost"]
        assert round(sum(m["total_cost"] for m in by_model), 9) == kpis["total_llm_cost"]

    def test_the_model_table_adds_up_to_the_headline_calls(self, engine, api):
        mixed_population(engine)
        conditions = _analytics.model_call_filters(PROJECT_ID, *WINDOW)
        models = overview(api)._models(PROJECT_ID, conditions)
        kpis = overview(api)._kpis(PROJECT_ID, _analytics.base_filters(PROJECT_ID, *WINDOW), None, conditions)

        assert sum(m["calls"] for m in models) == kpis["llm_calls"]
        assert {m["model_name"] for m in models} == {"gpt-x", "text-embedding-3-small", "own-gpt"}

    def test_an_embedding_event_with_input_tokens_and_no_output_keeps_its_tokens_and_row(self, engine, api):
        call(engine, 1, model_name="text-embedding-3-small", input_tokens=50, billable_input_tokens=50,
             output_tokens=0, cost_nano_usd=100)
        conditions = _analytics.model_call_filters(PROJECT_ID, *WINDOW)

        models = overview(api)._models(PROJECT_ID, conditions)
        kpis = overview(api)._kpis(PROJECT_ID, _analytics.base_filters(PROJECT_ID, *WINDOW), None, conditions)

        assert [(m["model_name"], m["calls"]) for m in models] == [("text-embedding-3-small", 1)]
        assert kpis["total_tokens"] == 50

    def test_people_metrics_still_leave_out_the_system_account_and_automated_runs(self, engine, api):
        mixed_population(engine)
        kpis = overview(api)._kpis(
            PROJECT_ID, _analytics.base_filters(PROJECT_ID, *WINDOW), None,
            _analytics.model_call_filters(PROJECT_ID, *WINDOW),
        )
        # users 1 and 2 are manual; the system account and the scheduled user 3 are not people
        assert kpis["unique_users"] == 2
        assert [u["user_id"] for u in leaderboard(api)] == [1, 2]

    def test_events_outside_the_window_are_not_counted(self, engine, api):
        mixed_population(engine)
        kpis = overview(api)._kpis(
            PROJECT_ID, _analytics.base_filters(PROJECT_ID, *WINDOW), None,
            _analytics.model_call_filters(PROJECT_ID, *WINDOW),
        )
        assert kpis["total_tokens"] < 999

    def test_the_daily_calls_add_up_to_the_headline_calls_including_a_day_with_only_system_calls(
            self, engine, api):
        mixed_population(engine)
        call(engine, SYSTEM_USER, seconds=86400, trigger_source="index", model_name="text-embedding-3-small")
        window = (WINDOW_FROM, WINDOW_TO)
        people = _analytics.base_filters(PROJECT_ID, *window)
        models = _analytics.model_call_filters(PROJECT_ID, *window)

        days = overview(api)._daily_activity(people, None, models)
        kpis = overview(api)._kpis(PROJECT_ID, people, None, models)

        assert sum(d["llm_calls"] for d in days) == kpis["llm_calls"] == 6
        assert [d["date"] for d in days] == sorted(d["date"] for d in days)

    def test_one_event_is_counted_exactly_once_across_types(self, engine, api):
        # AC 17: llm, tool, agent and pipeline rows of one period, each counted once
        call(engine, 1, run_id="agent-run", root_entity_type="application", root_entity_id=5)
        call(engine, 1, seconds=1, run_id="agent-run", root_entity_type="application", root_entity_id=5)
        call(engine, 1, seconds=2, run_id="agent-run", event_type="tool", tool_name="t", model_name=None,
             root_entity_type="application", root_entity_id=5)
        call(engine, 1, seconds=3, run_id="pipe-run", root_entity_type="pipeline", root_entity_id=6)
        people = _analytics.base_filters(PROJECT_ID, *WINDOW)
        models = _analytics.model_call_filters(PROJECT_ID, *WINDOW)

        kpis = overview(api)._kpis(PROJECT_ID, people, None, models)
        top = leaderboard(api)[0]

        assert (kpis["llm_calls"], kpis["tool_runs"], kpis["agent_runs"]) == (3, 1, 2)
        assert top["ai_events"] == top["llm_calls"] + top["tool_runs"] == 4


class TestLeaderboardScore:
    def test_failed_events_count_and_automated_runs_and_the_system_account_do_not(self, engine, api):
        call(engine, 1, is_error=True)
        call(engine, 1, seconds=1)
        assert leaderboard(api)[0]["ai_events"] == 2
        call(engine, 2, seconds=2, trigger_source="webhook")
        call(engine, SYSTEM_USER, seconds=3)
        assert [u["user_id"] for u in leaderboard(api)] == [1]

    def test_the_score_is_the_number_of_llm_and_tool_events(self, engine, api):
        call(engine, 1)
        call(engine, 1, seconds=1, event_type="tool", tool_name="t", model_name=None)
        row = leaderboard(api)[0]
        assert row["ai_events"] == row["llm_calls"] + row["tool_runs"] == 2

    def test_the_system_account_gets_a_name_where_totals_include_it(self):
        assert _analytics.user_fields({}, 0) == {"user_email": "System", "user_deleted": False}

"""A run starts when its first call started, not when it finished (#6997).

usage_event.ts is stamped once a call completes, so a call started at ts - duration_ms. The run
spans from the earliest such start to the latest ts. The real SQL runs against an in-memory usage_event.
"""
import datetime

import pytest

from usage.methods import _analytics
from usage.methods import _skill_analytics as sa

from unit.test_7033_agent_analytics_tables import (  # noqa: F401
    AGENT_ID, PROJECT_ID, RUN_ID, TS, WINDOW, api, call, core, engine, run_detail, run_page,
)

SKILL_ID = 174
MS = datetime.timedelta(milliseconds=1)


def parse(value):
    return _analytics.as_utc(datetime.datetime.fromisoformat(value))


def at(seconds):
    return _analytics.as_utc(TS + datetime.timedelta(seconds=seconds))


@pytest.fixture
def skill_meta(monkeypatch):
    monkeypatch.setattr(_analytics, "application_meta", lambda project_id, refs=(): {})
    monkeypatch.setattr(sa, "fetch_skill_meta", lambda project_id, skill_refs=(), conversation_uuids=(): ({}, {}))


def skill_call(engine, seconds=0, **values):  # noqa: F811
    call(engine, "skill-run", seconds, root_entity_type="skill", root_entity_id=SKILL_ID,
         entity_type="skill", entity_id=SKILL_ID, entity_name="summarizer (base)", **values)


class TestRunDetail:
    def test_a_single_call_run_lasts_as_long_as_the_call(self, api, engine, monkeypatch):  # noqa: F811
        call(engine, RUN_ID, duration_ms=2_337)

        body = run_detail(api, monkeypatch)

        assert body["duration_ms"] == 2_337
        assert parse(body["ended_at"]) == at(0)
        assert parse(body["started_at"]) == at(0) - 2_337 * MS

    def test_a_run_starts_at_the_earliest_call_start_and_ends_at_the_latest_end(
            self, api, engine, monkeypatch,  # noqa: F811
    ):
        call(engine, RUN_ID, duration_ms=3_000)
        call(engine, RUN_ID, seconds=4, event_type="tool", model_name=None, tool_name="search", duration_ms=500)
        # Finished last but started first: a sub-agent call spanning the others
        call(engine, RUN_ID, seconds=6, duration_ms=10_000)

        body = run_detail(api, monkeypatch)

        assert parse(body["started_at"]) == at(-4)
        assert parse(body["ended_at"]) == at(6)
        assert body["duration_ms"] == 10_000

    def test_the_first_calls_duration_is_counted(self, api, engine, monkeypatch):  # noqa: F811
        call(engine, RUN_ID, duration_ms=3_638)
        call(engine, RUN_ID, seconds=3, duration_ms=2_000)

        assert run_detail(api, monkeypatch)["duration_ms"] == 6_638

    def test_an_event_without_a_duration_starts_when_it_was_recorded(self, api, engine, monkeypatch):  # noqa: F811
        call(engine, RUN_ID, seconds=-1, event_type="skill", model_name=None, duration_ms=None)
        call(engine, RUN_ID, seconds=2, duration_ms=1_000)

        body = run_detail(api, monkeypatch)

        assert parse(body["started_at"]) == at(-1)
        assert body["duration_ms"] == 3_000

    def test_the_response_keys_are_unchanged(self, api, engine, monkeypatch):  # noqa: F811
        call(engine, RUN_ID, duration_ms=1_000)

        assert set(run_detail(api, monkeypatch)) == {
            "run_id", "root_entity_type", "root_entity_id", "root_entity_name", "root_entity_version_ids",
            "user_id", "started_at", "ended_at", "duration_ms", "kpis", "llm_calls", "tool_calls",
            "skill_events", "sub_agents", "root_entity_versions",
        }


class TestRunListsAgreeWithDetail:
    def test_the_agent_runs_list_starts_where_the_detail_does(self, api, engine, monkeypatch):  # noqa: F811
        call(engine, RUN_ID, duration_ms=2_500)
        call(engine, RUN_ID, seconds=2, duration_ms=1_000)

        row = run_page(api, monkeypatch)["rows"][0]

        assert parse(row["started_at"]) == at(-2.5)
        assert parse(row["started_at"]) == parse(run_detail(api, monkeypatch)["started_at"])

    def test_the_agent_runs_list_sorts_by_the_true_start(self, api, engine, monkeypatch):  # noqa: F811
        call(engine, "run-short", seconds=1, duration_ms=100)
        call(engine, "run-long", seconds=2, duration_ms=5_000)

        rows = run_page(api, monkeypatch, sort_by="started_at", sort_order="asc")["rows"]

        assert [r["run_id"] for r in rows] == ["run-long", "run-short"]

    def test_the_skill_recent_runs_start_at_the_first_call_start(self, engine, skill_meta):  # noqa: F811
        skill_call(engine, duration_ms=1_500)
        skill_call(engine, seconds=1, duration_ms=500)

        run = sa.build_skill_detail(PROJECT_ID, SKILL_ID, PROJECT_ID, *WINDOW)["recent_runs"][0]

        assert parse(run["started_at"]) == at(-1.5)

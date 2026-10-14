"""Skill usage on the Overview, Costs, Tokens, Agent detail and Users analytics tabs (#7030).

A skill run is a distinct run_id of the llm/tool rows rooted at a skill; an in-agent use is one
event_type='skill' row under an agent root. The real SQL runs against an in-memory usage_event.
"""
import datetime
import sys
import types

import pytest
from sqlalchemy import Date, func

from fixtures.usage_event_table import FakeUsageEvent, drop_engine, insert_row, install_engine, load_api_module
from usage.methods import _analytics
from usage.methods import _skill_analytics as sa

PROJECT_ID = 2
PUBLIC_PROJECT_ID = 1
SKILL_ID = 174
OTHER_SKILL_ID = 180
AGENT_ID = 111
OTHER_AGENT_ID = 112
TS = datetime.datetime(2026, 10, 8, 12, 0, 0)
WINDOW = (TS - datetime.timedelta(days=2), TS + datetime.timedelta(days=2))
API_MODULES = ("analytics", "analytics_costs", "analytics_agent_detail", "analytics_users")


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    monkeypatch.setattr(_analytics, "application_meta", lambda project_id, refs=(): {
        (PROJECT_ID, AGENT_ID): {"id": AGENT_ID, "name": "reviewer", "kind": "agent"},
    })
    monkeypatch.setattr(_analytics, "model_display_names", lambda project_id: {})
    monkeypatch.setattr(_analytics, "label_users", lambda user_ids, stored=None: {
        11: "ann@example.com", 12: "bob@example.com",
    })
    monkeypatch.setattr(_analytics, "outsider_admin_ids", lambda project_id, conditions: set())
    monkeypatch.setattr(_analytics, "project_member_count", lambda project_id, unique_users=0: 5)
    monkeypatch.setattr(_analytics, "day_expr", lambda: func.date(FakeUsageEvent.ts, type_=Date))
    yield db_engine
    drop_engine(db_engine)


@pytest.fixture
def meta(monkeypatch):
    calls = []
    state = {"calls": calls, "skills": {
        (PROJECT_ID, SKILL_ID): {"id": SKILL_ID, "project_id": PROJECT_ID, "name": "summarizer"},
        (PUBLIC_PROJECT_ID, SKILL_ID): {"id": SKILL_ID, "project_id": PUBLIC_PROJECT_ID, "name": "catalog summarizer"},
    }}

    def skill_meta(project_id, skill_refs=(), conversation_uuids=()):
        calls.append(list(skill_refs))
        skills = state["skills"]
        return None if skills is None else {ref: skills[ref] for ref in skill_refs if ref in skills}, {}

    monkeypatch.setattr(sa, "fetch_skill_meta", skill_meta)
    yield state


@pytest.fixture
def api(monkeypatch, engine):
    modules = types.SimpleNamespace(**{name: load_api_module(monkeypatch, name) for name in API_MODULES})
    yield modules
    for name in API_MODULES:
        sys.modules.pop(f"usage.api.v2.{name}", None)


def insert(engine, seconds=0, **values):
    row = {
        "ts": TS + datetime.timedelta(seconds=seconds), "project_id": PROJECT_ID, "user_id": 11,
        "run_id": "run-1", "event_type": "llm",
        "root_entity_type": "skill", "root_entity_id": SKILL_ID, "root_entity_version_id": 252,
        "root_entity_project_id": PROJECT_ID, "entity_type": "skill", "entity_id": SKILL_ID,
        "entity_name": "summarizer (base)", "model_name": "gpt-x", "is_error": False,
    }
    row.update(values)
    insert_row(engine, **row)


def agent_call(engine, seconds=0, **values):
    row = {
        "run_id": "agent-run-1", "root_entity_type": "application", "root_entity_id": AGENT_ID,
        "entity_type": "application", "entity_id": AGENT_ID, "entity_name": "reviewer (base)",
    }
    row.update(values)
    insert(engine, seconds, **row)


def use(engine, seconds=0, **values):
    row = {
        "event_type": "skill", "root_entity_type": "application", "root_entity_id": AGENT_ID,
        "entity_name": "summarizer", "entity_version_id": 252, "model_name": None, "run_id": "agent-run-1",
        "meta": {"source": "load_skill", "outcome": "loaded"},
    }
    row.update(values)
    insert(engine, seconds, **row)


def mixed_usage(engine):
    """Two runs of the local skill (one scheduled, by another user), a Catalog skill sharing its id,
    an agent run that loads the skill twice and mentions another, plus an unknown load_skill name."""
    insert(engine, input_tokens=100, billable_input_tokens=100, output_tokens=20, cost_nano_usd=4_000,
           input_cost_nano_usd=3_000, output_cost_nano_usd=1_000)
    insert(engine, seconds=5, input_tokens=50, billable_input_tokens=50, output_tokens=10, cost_nano_usd=2_000,
           input_cost_nano_usd=1_500, output_cost_nano_usd=500)
    insert(engine, seconds=6, event_type="tool", tool_name="search", cost_nano_usd=300)
    insert(engine, seconds=60, run_id="run-2", user_id=12, trigger_source="schedule", input_tokens=7,
           billable_input_tokens=7, cost_nano_usd=70, input_cost_nano_usd=70)
    insert(engine, seconds=90, run_id="run-catalog", root_entity_project_id=PUBLIC_PROJECT_ID, input_tokens=500,
           billable_input_tokens=500, cost_nano_usd=9_000, input_cost_nano_usd=9_000)
    agent_call(engine, seconds=3, input_tokens=900, billable_input_tokens=900, cost_nano_usd=70_000)
    use(engine, seconds=1)
    use(engine, seconds=2, meta={"source": "mention", "outcome": "loaded"})
    use(engine, seconds=4, entity_id=None, entity_name="nope", meta={"source": "load_skill", "outcome": "unknown_skill"})
    use(engine, seconds=7, entity_id=OTHER_SKILL_ID, entity_name="pirate")


def overview_conditions():
    return _analytics.base_filters(PROJECT_ID, *WINDOW)


def cost_conditions():
    return overview_conditions() + [_analytics.UsageEvent.event_type == _analytics.EVENT_LLM]


def agent_conditions(entity_id=AGENT_ID):
    return overview_conditions() + [_analytics.is_agent_row(), _analytics.UsageEvent.root_entity_id == entity_id]


def keyed(rows):
    return {(r["skill_project_id"], r["skill_id"]): r for r in rows}


class TestSkillRunsExpr:
    def test_one_run_of_many_calls_is_one_run(self, engine):
        insert(engine)
        insert(engine, seconds=1)
        insert(engine, seconds=2)
        insert(engine, seconds=3, event_type="tool", tool_name="search")

        row = _analytics.fetch_one(_analytics.select_from([_analytics.skill_runs_expr().label("runs")],
                                                          overview_conditions()))

        assert row["runs"] == 1

    def test_agent_rows_and_in_agent_uses_are_not_skill_runs(self, engine):
        agent_call(engine)
        use(engine)

        row = _analytics.fetch_one(_analytics.select_from([_analytics.skill_runs_expr().label("runs")],
                                                          _analytics.base_filters(
                                                              PROJECT_ID, *WINDOW, event_types=sa.SKILL_EVENT_TYPES,
                                                          )))

        assert row["runs"] == 0

    def test_a_skill_root_without_an_id_is_not_a_run(self, engine):
        insert(engine, root_entity_id=None)

        row = _analytics.fetch_one(_analytics.select_from([_analytics.skill_runs_expr().label("runs")],
                                                          overview_conditions()))

        assert row["runs"] == 0

    def test_matches_the_skills_tab_summary(self, engine, meta):
        mixed_usage(engine)

        row = _analytics.fetch_one(_analytics.select_from([_analytics.skill_runs_expr().label("runs")],
                                                          overview_conditions()))

        assert row["runs"] == sa.build_skill_leaderboard(PROJECT_ID, *WINDOW)["summary"]["runs"] == 3


class TestOverview:
    def test_kpis_count_every_skill_run_scheduled_included(self, engine, api):
        mixed_usage(engine)

        kpis = api.analytics.PromptLibAPI._kpis(PROJECT_ID, overview_conditions())

        assert (kpis["skill_runs"], kpis["agent_runs"]) == (3, 1)

    def test_kpis_respect_project_and_window(self, engine, api):
        mixed_usage(engine)
        insert(engine, project_id=3, run_id="elsewhere")
        insert(engine, seconds=-10 * 86400, run_id="long-ago")

        kpis = api.analytics.PromptLibAPI._kpis(PROJECT_ID, overview_conditions())
        later = api.analytics.PromptLibAPI._kpis(
            PROJECT_ID, _analytics.base_filters(PROJECT_ID, TS + datetime.timedelta(days=1), WINDOW[1]),
        )

        assert (kpis["skill_runs"], later["skill_runs"]) == (3, 0)

    def test_daily_activity_puts_skill_runs_on_their_day(self, engine, api):
        insert(engine)
        insert(engine, seconds=86400, run_id="run-next-day")
        insert(engine, seconds=86401, run_id="run-next-day-2")
        agent_call(engine, seconds=86400)

        days = api.analytics.PromptLibAPI._daily_activity(overview_conditions())

        assert [(d["date"], d["skill_runs"], d["agent_runs"]) for d in days] == [
            ("2026-10-08", 1, 0), ("2026-10-09", 2, 1),
        ]

    def test_leaderboard_counts_manual_skill_runs_and_keeps_ai_events(self, engine, api):
        mixed_usage(engine)

        users = {u["user_id"]: u for u in api.analytics.PromptLibAPI._top_ai_users(PROJECT_ID, overview_conditions())}

        assert set(users) == {11}
        ann = users[11]
        assert (ann["skill_runs"], ann["agent_runs"]) == (2, 1)
        assert ann["ai_events"] == ann["llm_calls"] + ann["tool_runs"] == 5


class TestCostBySkill:
    def test_groups_by_root_project_and_skill(self, engine, api, meta):
        mixed_usage(engine)

        rows = api.analytics_costs.PromptLibAPI._by_skill(PROJECT_ID, cost_conditions())

        assert [(r["skill_project_id"], r["skill_id"]) for r in rows] == [
            (PUBLIC_PROJECT_ID, SKILL_ID), (PROJECT_ID, SKILL_ID),
        ]
        local = keyed(rows)[(PROJECT_ID, SKILL_ID)]
        assert (local["skill_name"], local["deleted"], local["calls"]) == ("summarizer", False, 3)
        assert keyed(rows)[(PUBLIC_PROJECT_ID, SKILL_ID)]["skill_name"] == "catalog summarizer"

    def test_sums_raw_tokens_and_split_costs_of_llm_rows_only(self, engine, api, meta):
        mixed_usage(engine)

        local = keyed(api.analytics_costs.PromptLibAPI._by_skill(PROJECT_ID, cost_conditions()))[(PROJECT_ID, SKILL_ID)]

        assert (local["input_tokens"], local["output_tokens"], local["total_tokens"]) == (157, 30, 187)
        assert local["total_cost"] == pytest.approx(0.00000607)
        assert (local["input_cost"], local["output_cost"]) == (pytest.approx(0.00000457), pytest.approx(0.0000015))
        assert set(local) >= {"cache_read_cost", "cache_creation_cost", "cache_read_tokens", "cache_creation_tokens",
                              "below_resolution"}

    def test_input_tokens_are_raw_not_billable(self, engine, api, meta):
        insert(engine, input_tokens=130, billable_input_tokens=100, cache_read_tokens=30)

        local = api.analytics_costs.PromptLibAPI._by_skill(PROJECT_ID, cost_conditions())[0]

        assert (local["input_tokens"], local["cache_read_tokens"], local["total_tokens"]) == (130, 30, 130)

    def test_by_skill_and_by_agent_are_disjoint(self, engine, api, meta):
        mixed_usage(engine)
        costs = api.analytics_costs.PromptLibAPI

        skills = costs._by_skill(PROJECT_ID, cost_conditions())
        agents = costs._by_agent(PROJECT_ID, cost_conditions())
        kpis = costs._kpis(cost_conditions())

        assert [a["entity_id"] for a in agents] == [AGENT_ID]
        assert sum(r["total_tokens"] for r in skills) + sum(a["total_tokens"] for a in agents) == kpis["total_tokens"]

    def test_respects_project_and_window(self, engine, api, meta):
        insert(engine, project_id=3, run_id="elsewhere", cost_nano_usd=5)
        insert(engine, seconds=-10 * 86400, run_id="long-ago", cost_nano_usd=5)

        assert api.analytics_costs.PromptLibAPI._by_skill(PROJECT_ID, cost_conditions()) == []

    def test_the_tail_collapses_into_other_skills(self, engine, api, meta):
        for skill_id in range(1, 23):
            insert(engine, run_id=f"run-{skill_id}", root_entity_id=skill_id, entity_id=skill_id,
                   entity_name=f"skill-{skill_id} (v1)", cost_nano_usd=1_000 - skill_id, input_tokens=1)

        rows = api.analytics_costs.PromptLibAPI._by_skill(PROJECT_ID, cost_conditions())

        assert len(rows) == 21
        other = rows[-1]
        assert (other["skill_name"], other["is_other"], other["skill_id"], other["skill_project_id"]) == (
            "Other skills", True, None, None,
        )
        assert (other["calls"], other["input_tokens"], other["deleted"]) == (2, 2, False)
        assert [ref[1] for ref in meta["calls"][-1]] == list(range(1, 21))

    def test_unanswered_lookup_falls_back_to_the_unversioned_stored_name(self, engine, api, meta):
        insert(engine, root_entity_id=OTHER_SKILL_ID, entity_id=OTHER_SKILL_ID, entity_name="pirate (v1)")
        meta["skills"] = None

        row = api.analytics_costs.PromptLibAPI._by_skill(PROJECT_ID, cost_conditions())[0]

        assert (row["skill_name"], row["deleted"]) == ("pirate", False)

    def test_a_skill_core_cannot_find_is_deleted(self, engine, api, meta):
        insert(engine, root_entity_id=OTHER_SKILL_ID, entity_id=OTHER_SKILL_ID, entity_name="pirate (v1)")

        row = api.analytics_costs.PromptLibAPI._by_skill(PROJECT_ID, cost_conditions())[0]

        assert (row["skill_name"], row["deleted"]) == ("pirate", True)


def agent_skills_section(api):
    detail = api.analytics_agent_detail.PromptLibAPI
    return detail._skills(PROJECT_ID, detail._skill_conditions(PROJECT_ID, AGENT_ID, *WINDOW))


def agent_skills(api):
    return agent_skills_section(api)[2]


class TestAgentDetailSkills:
    def test_counts_this_agents_uses_per_skill(self, engine, api, meta):
        mixed_usage(engine)
        use(engine, seconds=8, run_id="agent-run-2")

        rows = agent_skills(api)

        assert [(r["skill_id"], r["uses"], r["runs"]) for r in rows] == [(SKILL_ID, 3, 2), (OTHER_SKILL_ID, 1, 1)]
        assert [(r["skill_name"], r["deleted"]) for r in rows] == [("summarizer", False), ("pirate", True)]

    def test_a_deleted_skill_keeps_its_stored_name_verbatim(self, engine, api, meta):
        use(engine, entity_id=OTHER_SKILL_ID, entity_name="pirate (beta)")

        assert [(r["skill_name"], r["deleted"]) for r in agent_skills(api)] == [("pirate (beta)", True)]

    def test_another_agents_uses_never_count(self, engine, api, meta):
        use(engine, root_entity_id=OTHER_AGENT_ID, run_id="other-agent-run")

        assert agent_skills(api) == []

    def test_an_unknown_skill_name_is_not_listed(self, engine, api, meta):
        use(engine, entity_id=None, entity_name="nope")

        assert agent_skills(api) == []

    def test_a_catalog_skill_sharing_the_id_is_its_own_row(self, engine, api, meta):
        use(engine)
        use(engine, seconds=1, meta={"source": "load_skill", "skill_project_id": PUBLIC_PROJECT_ID})

        rows = keyed(agent_skills(api))

        assert set(rows) == {(PROJECT_ID, SKILL_ID), (PUBLIC_PROJECT_ID, SKILL_ID)}
        assert rows[(PUBLIC_PROJECT_ID, SKILL_ID)]["skill_name"] == "catalog summarizer"

    def test_respects_project_and_window(self, engine, api, meta):
        use(engine, project_id=3)
        use(engine, seconds=-10 * 86400)

        assert agent_skills(api) == []

    def test_an_evaluation_cases_uses_never_count(self, engine, api, meta):
        agent_call(engine, run_id="eval-run-1", entity_type="evaluation", entity_id=7)
        use(engine, seconds=1, run_id="eval-run-1")

        assert agent_skills(api) == []

    def test_a_plain_run_counts_beside_an_evaluation(self, engine, api, meta):
        agent_call(engine, run_id="eval-run-1", entity_type="evaluation", entity_id=7)
        use(engine, seconds=1, run_id="eval-run-1")
        agent_call(engine, seconds=2, run_id=None, entity_type="evaluation", entity_id=8)
        agent_call(engine, seconds=2)
        use(engine, seconds=3)

        assert [(r["skill_id"], r["uses"], r["runs"]) for r in agent_skills(api)] == [(SKILL_ID, 1, 1)]

    def test_a_use_without_a_run_id_still_counts_beside_an_evaluation(self, engine, api, meta):
        agent_call(engine, run_id="eval-run-1", entity_type="evaluation", entity_id=7)
        use(engine, seconds=1, run_id=None)

        assert [(r["skill_id"], r["uses"]) for r in agent_skills(api)] == [(SKILL_ID, 1)]

    def test_another_agents_evaluation_leaves_this_agents_run_alone(self, engine, api, meta):
        agent_call(engine, root_entity_id=OTHER_AGENT_ID, entity_type="evaluation", entity_id=7)
        use(engine, seconds=1)

        assert [(r["skill_id"], r["uses"]) for r in agent_skills(api)] == [(SKILL_ID, 1)]

    def test_an_eval_run_scope_keeps_its_cases_uses(self, engine, api, meta):
        agent_call(engine, run_id="eval-run-1", entity_type="evaluation", entity_id=7)
        use(engine, seconds=1, run_id="eval-run-1")
        detail = api.analytics_agent_detail.PromptLibAPI
        run_scope = _analytics.RunScope(eval_run_id=7, platform_run_id="eval-run-1")

        _, _, rows = detail._skills(PROJECT_ID, detail._skill_conditions(PROJECT_ID, AGENT_ID, *WINDOW, run_scope))

        assert [(r["skill_id"], r["uses"]) for r in rows] == [(SKILL_ID, 1)]

    def test_every_skill_is_counted_when_the_list_is_capped(self, engine, api, meta, monkeypatch):
        monkeypatch.setattr(api.analytics_agent_detail, "_SKILLS_LIMIT", 2)
        use(engine, entity_id=OTHER_SKILL_ID, entity_name="pirate")
        use(engine, seconds=1, entity_id=OTHER_SKILL_ID, entity_name="pirate")
        for offset, skill_id in enumerate((SKILL_ID + 2, SKILL_ID + 1, SKILL_ID), start=2):
            use(engine, seconds=offset, entity_id=skill_id, entity_name=f"skill {skill_id}")

        total, truncated, rows = agent_skills_section(api)

        assert (total, truncated, len(rows)) == (4, True, 2)
        assert rows[0]["skill_id"] == OTHER_SKILL_ID

    def test_a_list_within_the_cap_is_not_truncated(self, engine, api, meta):
        mixed_usage(engine)

        total, truncated, rows = agent_skills_section(api)

        assert (total, truncated, len(rows)) == (2, False, 2)

    def test_use_rows_stay_out_of_the_other_sections(self, engine, api, meta):
        agent_call(engine)
        agent_call(engine, seconds=1, event_type="tool", tool_name="search")
        detail = api.analytics_agent_detail.PromptLibAPI
        before = (detail._kpis(agent_conditions(), AGENT_ID)[1], detail._users(agent_conditions()),
                  detail._tools(agent_conditions()))

        use(engine, seconds=2, tool_name="search", billable_input_tokens=50)
        after = (detail._kpis(agent_conditions(), AGENT_ID)[1], detail._users(agent_conditions()),
                 detail._tools(agent_conditions()))

        assert after == before

    def test_get_returns_the_skills_section(self, engine, api, meta, monkeypatch):
        mixed_usage(engine)
        monkeypatch.setattr(api.analytics_agent_detail, "request", types.SimpleNamespace(args={
            "entity_id": str(AGENT_ID),
        }))
        monkeypatch.setattr(_analytics, "parse_date_range", lambda args, run_scope=None: WINDOW)

        body, status = api.analytics_agent_detail.PromptLibAPI().get(PROJECT_ID)

        assert status == 200
        assert [(s["skill_id"], s["uses"]) for s in body["skills"]] == [(SKILL_ID, 2), (OTHER_SKILL_ID, 1)]
        assert (body["skills_total"], body["skills_truncated"]) == (2, False)


class TestUsersSkillRuns:
    @pytest.fixture
    def users(self, api, monkeypatch):
        monkeypatch.setattr(api.analytics_users, "request", types.SimpleNamespace(args={}))
        monkeypatch.setattr(_analytics, "parse_date_range", lambda args, run_scope=None: WINDOW)

        def fetch():
            body, status = api.analytics_users.PromptLibAPI().get(PROJECT_ID)
            assert status == 200
            return {r["user_id"]: r for r in body["rows"]}

        yield fetch

    def test_each_user_has_their_manual_skill_runs(self, engine, users):
        mixed_usage(engine)
        insert(engine, seconds=120, run_id="run-3", user_id=12)

        rows = users()

        assert (rows[11]["skill_events"], rows[11]["agent_events"]) == (2, 1)
        assert rows[12]["skill_events"] == 1

    def test_respects_project_and_window(self, engine, users):
        insert(engine)
        insert(engine, project_id=3, run_id="elsewhere")
        insert(engine, seconds=-10 * 86400, run_id="long-ago")

        assert users()[11]["skill_events"] == 1

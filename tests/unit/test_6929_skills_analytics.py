"""Skills analytics tab (#6929).

A skill run is the llm/tool rows rooted at the skill; an in-agent use is one event_type='skill'
row. The leaderboard and the detail count both, apart, and only runs carry tokens and cost.
The real SQL runs against an in-memory usage_event.
"""
import datetime
import sys
import types

import pytest
from sqlalchemy import func, select

from fixtures.usage_event_table import FakeUsageEvent, drop_engine, insert_row, install_engine
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
CONVERSATION_1 = "00000000-0000-0000-0000-000000000001"
CONVERSATION_2 = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    monkeypatch.setattr(_analytics, "application_meta", lambda project_id, refs=(): {
        (PROJECT_ID, AGENT_ID): {"id": AGENT_ID, "name": "reviewer", "kind": "agent"},
    })
    monkeypatch.setattr(_analytics, "model_display_names", lambda project_id: {"gpt-x": "GPT X"})
    monkeypatch.setattr(_analytics, "label_users", lambda user_ids, stored=None: {
        11: "ann@example.com", 12: "bob@example.com",
    })
    yield db_engine
    drop_engine(db_engine)


@pytest.fixture
def meta(monkeypatch):
    calls = []
    state = {"skills": {
        (PROJECT_ID, SKILL_ID): {
            "id": SKILL_ID, "project_id": PROJECT_ID, "name": "summarizer", "is_public": False,
            "versions": [{"id": 252, "name": "base"}, {"id": 258, "name": "model-x"}],
        },
        (PUBLIC_PROJECT_ID, SKILL_ID): {
            "id": SKILL_ID, "project_id": PUBLIC_PROJECT_ID, "name": "catalog summarizer",
            "is_public": True, "versions": [],
        },
    }}

    def skill_meta(project_id, skill_refs=(), conversation_uuids=()):
        calls.append((list(skill_refs), list(conversation_uuids)))
        skills = state["skills"]
        return (
            None if skills is None else {ref: skills[ref] for ref in skill_refs if ref in skills},
            {CONVERSATION_1: 1701},
        )

    monkeypatch.setattr(sa, "skill_meta", skill_meta)
    state["calls"] = calls
    yield state


def insert(engine, seconds=0, **values):
    row = {
        "ts": TS + datetime.timedelta(seconds=seconds), "project_id": PROJECT_ID, "user_id": 11,
        "run_id": "run-1", "conversation_id": CONVERSATION_1, "event_type": "llm",
        "root_entity_type": "skill", "root_entity_id": SKILL_ID, "root_entity_version_id": 252,
        "root_entity_project_id": PROJECT_ID, "entity_type": "skill", "entity_id": SKILL_ID,
        "entity_name": "summarizer (base)", "model_name": "gpt-x", "is_error": False,
    }
    row.update(values)
    insert_row(engine, **row)


def use(engine, seconds=0, **values):
    row = {
        "event_type": "skill", "root_entity_type": "application", "root_entity_id": AGENT_ID,
        "root_entity_version_id": 215, "entity_name": "summarizer", "entity_version_id": 252,
        "model_name": None, "run_id": "agent-run-1", "conversation_id": CONVERSATION_2,
        "meta": {"source": "load_skill", "outcome": "loaded", "est_body_tokens": 40},
    }
    row.update(values)
    insert(engine, seconds, **row)


def mixed_usage(engine):
    """Two runs of the skill (one failed), three in-agent uses, a Catalog skill sharing its id,
    an agent's own call, an unknown load_skill name and another skill used only in agents."""
    insert(engine, billable_input_tokens=100, input_tokens=100, output_tokens=20, cost_nano_usd=4_000,
           input_cost_nano_usd=3_000, output_cost_nano_usd=1_000, duration_ms=1_000)
    insert(engine, seconds=5, billable_input_tokens=50, input_tokens=50, output_tokens=10,
           cost_nano_usd=2_000, input_cost_nano_usd=1_500, output_cost_nano_usd=500, duration_ms=3_000)
    insert(engine, seconds=60, run_id="run-2", user_id=12, root_entity_version_id=258, model_name="haiku",
           billable_input_tokens=7, input_tokens=7, cost_nano_usd=70, input_cost_nano_usd=70,
           duration_ms=500, is_error=True, trigger_source="schedule", conversation_id=CONVERSATION_2)
    use(engine)
    use(engine, seconds=1, run_id="agent-run-2", user_id=12,
        meta={"source": "mention", "outcome": "loaded", "est_body_tokens": 20})
    use(engine, seconds=2, run_id="agent-run-3", root_entity_id=OTHER_AGENT_ID, entity_version_id=None)
    insert(engine, seconds=90, run_id="run-catalog", root_entity_project_id=PUBLIC_PROJECT_ID,
           root_entity_version_id=145, billable_input_tokens=500, cost_nano_usd=9_000, duration_ms=700)
    insert(engine, seconds=3, run_id="agent-run-1", root_entity_type="application", root_entity_id=AGENT_ID,
           entity_type="application", entity_id=AGENT_ID, entity_name="reviewer (base)",
           billable_input_tokens=900, cost_nano_usd=70_000)
    use(engine, seconds=4, entity_id=None, entity_name="nope", meta={"source": "load_skill", "outcome": "unknown_skill"})
    use(engine, seconds=6, entity_id=OTHER_SKILL_ID, entity_name="pirate", entity_version_id=270,
        meta={"source": "mention", "outcome": "loaded", "est_body_tokens": 4})


def leaderboard(**kwargs):
    return sa.skill_leaderboard(PROJECT_ID, *WINDOW, **kwargs)


def by_skill(result):
    return {(r["skill_project_id"], r["skill_id"]): r for r in result["rows"]}


class TestLeaderboard:
    def test_runs_and_in_agent_uses_are_counted_apart(self, engine, meta):
        mixed_usage(engine)

        row = by_skill(leaderboard())[(PROJECT_ID, SKILL_ID)]

        assert (row["runs"], row["in_agent_uses"]) == (2, 3)
        assert (row["users"], row["agents_using"], row["errors"]) == (2, 2, 1)
        assert row["avg_duration_ms"] == 1500.0
        assert row["avg_est_body_tokens"] == pytest.approx(33.3, abs=0.1)

    def test_only_runs_carry_tokens_and_cost(self, engine, meta):
        mixed_usage(engine)

        row = by_skill(leaderboard())[(PROJECT_ID, SKILL_ID)]

        assert row["total_tokens"] == 187
        assert row["cost"] == pytest.approx(0.00000607)
        assert (row["input_cost"], row["output_cost"]) == (pytest.approx(0.00000457), pytest.approx(0.0000015))

    def test_an_in_agent_row_with_tokens_still_adds_none(self, engine, meta):
        use(engine, billable_input_tokens=999, cost_nano_usd=999)

        row = by_skill(leaderboard())[(PROJECT_ID, SKILL_ID)]

        assert (row["runs"], row["in_agent_uses"], row["total_tokens"], row["cost"]) == (0, 1, 0, 0)

    def test_a_catalog_skill_sharing_the_id_is_its_own_row(self, engine, meta):
        mixed_usage(engine)

        rows = by_skill(leaderboard())

        assert set(rows) == {(PROJECT_ID, SKILL_ID), (PUBLIC_PROJECT_ID, SKILL_ID), (PROJECT_ID, OTHER_SKILL_ID)}
        assert rows[(PUBLIC_PROJECT_ID, SKILL_ID)]["skill_name"] == "catalog summarizer"
        assert rows[(PUBLIC_PROJECT_ID, SKILL_ID)]["is_public"] is True
        assert rows[(PUBLIC_PROJECT_ID, SKILL_ID)]["runs"] == 1

    def test_the_agent_and_an_unknown_skill_name_are_not_skills(self, engine, meta):
        mixed_usage(engine)

        assert leaderboard()["total"] == 3

    def test_summary_covers_the_period(self, engine, meta):
        mixed_usage(engine)

        summary = leaderboard(search="pirate")["summary"]

        assert summary == {
            "skills_used": 3, "runs": 3, "in_agent_uses": 4, "total_tokens": 687,
            "cost": pytest.approx(0.00001507),
        }

    def test_search_matches_the_stored_name(self, engine, meta):
        mixed_usage(engine)

        result = leaderboard(search="PIRA")

        assert result["total"] == 1
        assert [r["skill_id"] for r in result["rows"]] == [OTHER_SKILL_ID]

    @pytest.mark.parametrize("sort_by, sort_order, expected", [
        ("runs", "desc", [(PROJECT_ID, SKILL_ID), (PUBLIC_PROJECT_ID, SKILL_ID), (PROJECT_ID, OTHER_SKILL_ID)]),
        ("in_agent_uses", "asc", [(PUBLIC_PROJECT_ID, SKILL_ID), (PROJECT_ID, OTHER_SKILL_ID), (PROJECT_ID, SKILL_ID)]),
        ("cost", "desc", [(PUBLIC_PROJECT_ID, SKILL_ID), (PROJECT_ID, SKILL_ID), (PROJECT_ID, OTHER_SKILL_ID)]),
        ("skill_name", "asc", [(PROJECT_ID, OTHER_SKILL_ID), (PUBLIC_PROJECT_ID, SKILL_ID), (PROJECT_ID, SKILL_ID)]),
        ("avg_duration_ms", "desc", [(PROJECT_ID, SKILL_ID), (PUBLIC_PROJECT_ID, SKILL_ID), (PROJECT_ID, OTHER_SKILL_ID)]),
    ])
    def test_sort(self, engine, meta, sort_by, sort_order, expected):
        mixed_usage(engine)

        rows = leaderboard(sort_by=sort_by, sort_order=sort_order)["rows"]

        assert [(r["skill_project_id"], r["skill_id"]) for r in rows] == expected

    def test_paging_is_server_side(self, engine, meta):
        mixed_usage(engine)

        result = leaderboard(limit=1, offset=1)

        assert result["total"] == 3
        assert [(r["skill_project_id"], r["skill_id"]) for r in result["rows"]] == [(PUBLIC_PROJECT_ID, SKILL_ID)]
        assert meta["calls"][-1][0] == [(PUBLIC_PROJECT_ID, SKILL_ID)]

    def test_the_window_bounds_the_rows(self, engine, meta):
        mixed_usage(engine)

        result = sa.skill_leaderboard(PROJECT_ID, TS + datetime.timedelta(days=1), TS + datetime.timedelta(days=2))

        assert result["total"] == 0 and result["summary"]["skills_used"] == 0

    def test_another_projects_rows_never_count(self, engine, meta):
        mixed_usage(engine)
        insert(engine, project_id=3, run_id="elsewhere", cost_nano_usd=1_000_000)

        row = by_skill(leaderboard())[(PROJECT_ID, SKILL_ID)]

        assert row["runs"] == 2


class TestDeletedSkill:
    def test_a_skill_core_cannot_find_is_deleted_with_its_last_name(self, engine, meta):
        mixed_usage(engine)

        row = by_skill(leaderboard())[(PROJECT_ID, OTHER_SKILL_ID)]

        assert (row["skill_name"], row["deleted"]) == ("pirate", True)

    def test_an_unanswered_lookup_marks_nothing_deleted(self, engine, meta):
        mixed_usage(engine)
        meta["skills"] = None

        rows = leaderboard()["rows"]

        assert not any(r["deleted"] for r in rows)
        assert by_skill({"rows": rows})[(PROJECT_ID, SKILL_ID)]["skill_name"] == "summarizer"


class TestReconciliation:
    def test_skill_cost_and_tokens_are_the_sum_over_skill_root_rows_and_a_subset_of_the_project(self, engine, meta):
        mixed_usage(engine)

        summary = leaderboard()["summary"]
        conditions = _analytics.base_filters(PROJECT_ID, *WINDOW)
        project = _analytics.fetch_one(select(
            func.sum(_analytics.total_tokens_expr()).label("tokens"),
            func.sum(FakeUsageEvent.cost_nano_usd).label("cost"),
        ).where(*conditions))
        skill_root = _analytics.fetch_one(select(
            func.sum(_analytics.total_tokens_expr()).label("tokens"),
            func.sum(FakeUsageEvent.cost_nano_usd).label("cost"),
        ).where(*conditions, FakeUsageEvent.root_entity_type == "skill"))

        assert summary["total_tokens"] == skill_root["tokens"] < project["tokens"]
        assert summary["cost"] == _analytics.cost_usd(skill_root["cost"])
        assert skill_root["cost"] < project["cost"]

    def test_runs_are_distinct_run_ids(self, engine, meta):
        mixed_usage(engine)

        rows = _analytics.fetch_all(select(FakeUsageEvent.run_id).where(
            FakeUsageEvent.root_entity_type == "skill", FakeUsageEvent.root_entity_id == SKILL_ID,
            FakeUsageEvent.root_entity_project_id == PROJECT_ID,
        ).distinct())

        assert by_skill(leaderboard())[(PROJECT_ID, SKILL_ID)]["runs"] == len(rows)


def detail():
    return sa.skill_detail(PROJECT_ID, SKILL_ID, PROJECT_ID, *WINDOW)


class TestDetail:
    def test_no_rows_is_none(self, engine, meta):
        assert detail() is None

    def test_kpis_match_the_leaderboard_row(self, engine, meta):
        mixed_usage(engine)

        kpis = detail()["kpis"]
        row = by_skill(leaderboard())[(PROJECT_ID, SKILL_ID)]

        assert {k: kpis[k] for k in row if k in kpis} == {k: row[k] for k in row if k in kpis}
        assert kpis["error_rate"] == 50.0

    def test_the_catalog_skill_is_not_mixed_in(self, engine, meta):
        mixed_usage(engine)

        result = sa.skill_detail(PROJECT_ID, SKILL_ID, PUBLIC_PROJECT_ID, *WINDOW)

        assert (result["skill_name"], result["kpis"]["runs"], result["kpis"]["in_agent_uses"]) == (
            "catalog summarizer", 1, 0,
        )

    def test_daily_trend(self, engine, meta):
        mixed_usage(engine)
        insert(engine, seconds=86_400, run_id="run-3", cost_nano_usd=1_000)

        daily = detail()["daily"]

        assert [(d["date"], d["runs"], d["in_agent_uses"], d["cost"]) for d in daily] == [
            ("2026-10-08", 2, 3, pytest.approx(0.00000607)), ("2026-10-09", 1, 0, pytest.approx(0.000001)),
        ]

    def test_versions_with_an_unknown_in_agent_version(self, engine, meta):
        mixed_usage(engine)

        versions = {v["version_id"]: v for v in detail()["by_version"]}

        assert (versions[252]["version_name"], versions[252]["runs"], versions[252]["in_agent_uses"]) == ("base", 1, 2)
        assert (versions[258]["version_name"], versions[258]["runs"]) == ("model-x", 1)
        assert (versions[None]["version_name"], versions[None]["in_agent_uses"]) == (None, 1)

    def test_models_count_llm_calls_of_runs(self, engine, meta):
        mixed_usage(engine)

        models = [(m["model_name"], m["display_name"], m["runs"], m["calls"]) for m in detail()["by_model"]]

        assert models == [("gpt-x", "GPT X", 1, 2), ("haiku", "haiku", 1, 1)]

    def test_users_and_trigger_sources(self, engine, meta):
        mixed_usage(engine)

        result = detail()

        assert [(u["user_email"], u["runs"], u["in_agent_uses"]) for u in result["by_user"]] == [
            ("ann@example.com", 1, 2), ("bob@example.com", 1, 1),
        ]
        assert sorted((t["trigger_source"], t["runs"], t["in_agent_uses"]) for t in result["by_trigger_source"]) == [
            ("manual", 1, 3), ("schedule", 1, 0),
        ]

    def test_top_agents_and_the_load_vs_mention_split(self, engine, meta):
        mixed_usage(engine)

        result = detail()

        assert [(a["entity_id"], a["entity_name"], a["uses"], a["deleted"]) for a in result["agents"]] == [
            (AGENT_ID, "reviewer", 2, False), (OTHER_AGENT_ID, f"Agent #{OTHER_AGENT_ID}", 1, True),
        ]
        assert result["by_source"] == [{"source": "load_skill", "uses": 2}, {"source": "mention", "uses": 1}]

    def test_a_skill_mentioned_inside_another_skill_run_is_a_use_not_an_agent(self, engine, meta):
        use(engine, root_entity_type="skill", root_entity_id=175, run_id="skill-run")

        result = detail()

        assert (result["kpis"]["in_agent_uses"], result["kpis"]["runs"], result["agents"]) == (1, 0, [])

    def test_recent_runs_link_to_their_conversation(self, engine, meta):
        mixed_usage(engine)

        runs = detail()["recent_runs"]

        assert [(r["run_id"], r["conversation_id"], r["is_error"], r["total_tokens"]) for r in runs] == [
            ("run-2", None, True, 7), ("run-1", 1701, False, 180),
        ]
        assert runs[1]["version_name"] == "base" and runs[1]["model_name"] == "GPT X"
        assert runs[1]["duration_ms"] == 4_000
        assert sorted(meta["calls"][-1][1]) == [CONVERSATION_1, CONVERSATION_2]


class TestSkillMeta:
    @pytest.fixture
    def rpc(self, monkeypatch):
        response = {}

        class Rpc:
            def timeout(self, _seconds):
                return self

            def elitea_core_usage_entity_meta(self, project_id, **kwargs):
                response["kwargs"] = kwargs
                if isinstance(response.get("reply"), Exception):
                    raise response["reply"]
                return response.get("reply")

        tools = sys.modules["tools"]
        monkeypatch.setattr(tools, "rpc_tools", types.SimpleNamespace(
            RpcMixin=lambda: types.SimpleNamespace(rpc=Rpc()),
        ), raising=False)
        yield response

    def test_skills_and_conversations_are_keyed(self, rpc):
        rpc["reply"] = {
            "scoped_skills": [{"id": 5, "project_id": 2, "name": "a"}],
            "conversations": [{"uuid": CONVERSATION_1, "id": 9}],
        }

        skills, conversations = sa.skill_meta(PROJECT_ID, [(2, 5), (2, 5)], [CONVERSATION_1])

        assert skills == {(2, 5): {"id": 5, "project_id": 2, "name": "a"}}
        assert conversations == {CONVERSATION_1: 9}
        assert rpc["kwargs"] == {"skill_refs": [[2, 5]], "conversation_uuids": [CONVERSATION_1]}

    @pytest.mark.parametrize("reply", [RuntimeError("down"), {"applications": []}])
    def test_a_failed_or_older_core_is_unknown_not_empty(self, rpc, reply):
        rpc["reply"] = reply

        skills, _ = sa.skill_meta(PROJECT_ID, [(2, 5)])

        assert skills is None

    def test_nothing_to_resolve_makes_no_call(self, rpc):
        assert sa.skill_meta(PROJECT_ID) == ({}, {})
        assert "kwargs" not in rpc

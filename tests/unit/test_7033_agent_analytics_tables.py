"""Versions, Models, Trigger Sources and Recent Runs of one agent/pipeline (#7033).

The real SQL runs against an in-memory usage_event; elitea_core answers through a fake RPC.
"""
import datetime
import json
import sys
import types

import pytest
from sqlalchemy import Date, event, func

from fixtures.usage_event_table import FakeUsageEvent, drop_engine, insert_row, install_engine, load_api_module
from usage.methods import _analytics

PROJECT_ID = 2
PUBLIC_PROJECT_ID = 1
AGENT_ID = 111
OTHER_AGENT_ID = 112
SKILL_ID = 174
TS = datetime.datetime(2026, 10, 8, 12, 0, 0)
WINDOW = (TS - datetime.timedelta(days=2), TS + datetime.timedelta(days=2))
CONVERSATION = "00000000-0000-0000-0000-000000000001"
RUN_ID = "67a08328-da8f-4c14-9bdb-1e1268d339e3"
API_MODULES = ("analytics_agent_detail", "analytics_agent_runs", "analytics_run_detail")


@pytest.fixture
def engine(monkeypatch):
    db_engine = install_engine(monkeypatch, _analytics)
    monkeypatch.setattr(_analytics, "model_display_names", lambda project_id: {"gpt-a": "GPT A", "gpt-b": "Alpha B"})
    monkeypatch.setattr(_analytics, "label_users", lambda user_ids, stored=None: {
        11: "ann@example.com", 12: "bob@example.com",
    })
    monkeypatch.setattr(_analytics, "day_expr", lambda: func.date(FakeUsageEvent.ts, type_=Date))
    monkeypatch.setattr(_analytics, "parse_date_range", lambda args, run_scope=None: WINDOW)
    yield db_engine
    drop_engine(db_engine)


@pytest.fixture
def core(monkeypatch):
    state = {
        "down": False,
        "calls": [],
        "versions": {31: ("renamed", AGENT_ID), 77: ("another agent's version", OTHER_AGENT_ID)},
        "skills": {(PROJECT_ID, SKILL_ID): [{"id": 31, "name": "skill v1"}]},
        "applications": {(PROJECT_ID, AGENT_ID): "pipeline", (PUBLIC_PROJECT_ID, AGENT_ID): "agent"},
        "conversations": {CONVERSATION: 1701},
    }

    class Rpc:
        def timeout(self, _seconds):
            return self

        def elitea_core_usage_entity_meta(self, project_id, **kwargs):
            state["calls"].append(kwargs)
            if state["down"]:
                raise RuntimeError("elitea_core is down")
            return {
                "versions": [
                    {"id": i, "name": state["versions"][i][0], "application_id": state["versions"][i][1]}
                    for i in kwargs.get("version_ids") or [] if i in state["versions"]
                ],
                "scoped_skills": [
                    {"id": i, "project_id": p, "name": "summarizer", "versions": state["skills"][(p, i)]}
                    for p, i in kwargs.get("skill_refs") or [] if (p, i) in state["skills"]
                ],
                "scoped_applications": [
                    {"id": i, "project_id": p, "kind": state["applications"][(p, i)]}
                    for p, i in kwargs.get("application_refs") or [] if (p, i) in state["applications"]
                ],
                "conversations": [
                    {"uuid": u, "id": state["conversations"][u]} for u in kwargs.get("conversation_uuids") or []
                    if u in state["conversations"]
                ],
            }

    monkeypatch.setattr(sys.modules["tools"], "rpc_tools", types.SimpleNamespace(
        RpcMixin=lambda: types.SimpleNamespace(rpc=Rpc()),
    ), raising=False)
    yield state


@pytest.fixture
def api(monkeypatch, engine, core):
    modules = types.SimpleNamespace(**{name: load_api_module(monkeypatch, name) for name in API_MODULES})
    yield modules
    for name in API_MODULES:
        sys.modules.pop(f"usage.api.v2.{name}", None)


def call(engine, run_id, seconds=0, **values):
    row = {
        "ts": TS + datetime.timedelta(seconds=seconds), "project_id": PROJECT_ID, "user_id": 11,
        "run_id": run_id, "event_type": "llm", "model_name": "gpt-a",
        "root_entity_type": "application", "root_entity_id": AGENT_ID, "root_entity_version_id": 31,
        "root_entity_project_id": PROJECT_ID, "entity_type": "application", "entity_id": AGENT_ID,
        "entity_name": "reviewer (base)", "billable_input_tokens": 100, "output_tokens": 10,
        "cost_nano_usd": 1_000, "is_error": False,
    }
    row.update(values)
    insert_row(engine, **row)


def child_call(engine, run_id, seconds=0, **values):
    call(engine, run_id, seconds, entity_id=AGENT_ID + 50, entity_name="helper (v1)", **values)


def detail(api, monkeypatch):
    monkeypatch.setattr(api.analytics_agent_detail, "request", types.SimpleNamespace(args={
        "entity_id": str(AGENT_ID),
    }))
    body, status = api.analytics_agent_detail.PromptLibAPI().get(PROJECT_ID)
    assert status == 200, body
    return body


def runs(api, monkeypatch, **args):
    monkeypatch.setattr(api.analytics_agent_runs, "request", types.SimpleNamespace(args={
        "entity_id": str(AGENT_ID), **args,
    }))
    return api.analytics_agent_runs.PromptLibAPI().get(PROJECT_ID)


def run_page(api, monkeypatch, **args):
    body, status = runs(api, monkeypatch, **args)
    assert status == 200, body
    return body


def run_detail(api, monkeypatch):
    monkeypatch.setattr(api.analytics_run_detail, "request", types.SimpleNamespace(args={"run_id": RUN_ID}))
    body, status = api.analytics_run_detail.PromptLibAPI().get(PROJECT_ID)
    assert status == 200, body
    return body


def mixed_usage(engine):
    call(engine, "run-manual-1", billable_input_tokens=200, cost_nano_usd=2_000)
    call(engine, "run-manual-1", seconds=1, event_type="tool", model_name=None, tool_name="search",
         billable_input_tokens=0, output_tokens=0, cost_nano_usd=0)
    call(engine, "run-manual-2", seconds=10, root_entity_version_id=32, entity_name="reviewer (draft)")
    child_call(engine, "run-scheduled", seconds=20, trigger_source=None, billable_input_tokens=300)
    child_call(engine, "run-scheduled", seconds=21, trigger_source=None, cost_nano_usd=5_000)
    call(engine, "run-scheduled", seconds=22, trigger_source="scheduled")
    call(engine, "run-webhook", seconds=30, trigger_source="webhook", user_id=12, model_name="gpt-b")
    call(engine, "run-eval", seconds=40, entity_type="evaluation", entity_id=7, billable_input_tokens=9_000)
    call(engine, "run-system", seconds=50, user_id=0, billable_input_tokens=9_000)
    call(engine, "run-other-agent", seconds=60, root_entity_id=OTHER_AGENT_ID, billable_input_tokens=9_000)


def totals(rows):
    return (
        sum(r["runs"] for r in rows),
        sum(r["total_tokens"] for r in rows),
        pytest.approx(sum(r["cost"] for r in rows)),
    )


class TestDetailReconciliation:
    def test_versions_partition_the_kpis(self, api, engine, monkeypatch):
        mixed_usage(engine)

        body = detail(api, monkeypatch)

        kpis = body["kpis"]
        assert totals(body["by_version"]) == (kpis["total_events"], kpis["total_tokens"], kpis["llm_cost"])
        assert [(v["version_id"], v["runs"], v["total_tokens"]) for v in body["by_version"]] == [
            (31, 3, 850), (32, 1, 110),
        ]

    def test_triggers_partition_the_kpis(self, api, engine, monkeypatch):
        mixed_usage(engine)

        body = detail(api, monkeypatch)

        kpis = body["kpis"]
        assert totals(body["by_trigger_source"]) == (kpis["total_events"], kpis["total_tokens"], kpis["llm_cost"])
        json.dumps(body)


class TestTriggerSources:
    def test_a_scheduled_run_with_null_children_counts_once_under_scheduled(self, api, engine, monkeypatch):
        mixed_usage(engine)

        triggers = detail(api, monkeypatch)["by_trigger_source"]

        assert [(t["trigger_source"], t["runs"], t["total_tokens"], t["cost"]) for t in triggers] == [
            ("manual", 2, 320, 0.000003), ("scheduled", 1, 530, 0.000007), ("webhook", 1, 110, 0.000001),
        ]


class TestModels:
    def test_a_multi_model_run_counts_once_in_each_model_row(self, api, engine, monkeypatch):
        call(engine, "run-1", billable_input_tokens=500)
        call(engine, "run-1", seconds=1)
        call(engine, "run-1", seconds=2, model_name="gpt-b")
        call(engine, "run-1", seconds=3, event_type="tool", model_name=None, tool_name="search")
        call(engine, "run-2", seconds=10)

        models = detail(api, monkeypatch)["by_model"]

        assert [(m["model_name"], m["display_name"], m["runs"], m["calls"], m["total_tokens"]) for m in models] == [
            ("gpt-a", "GPT A", 2, 3, 730), ("gpt-b", "Alpha B", 1, 1, 110),
        ]


class TestVersionNames:
    def test_names_fall_back_from_core_to_stored_name_to_id(self, api, engine, monkeypatch):
        call(engine, "run-1")
        call(engine, "run-2", seconds=1, root_entity_version_id=32, entity_name="reviewer (beta) (old draft)")
        child_call(engine, "run-3", seconds=2, root_entity_version_id=33)
        call(engine, "run-4", seconds=3, root_entity_version_id=None)

        versions = detail(api, monkeypatch)["by_version"]

        assert {v["version_id"]: v["version_name"] for v in versions} == {
            31: "renamed", 32: "old draft", 33: "Version #33", None: None,
        }
        assert versions[-1]["version_id"] is None


class TestIdentity:
    def test_kind_and_project_come_from_the_runs_root_project(self, api, engine, monkeypatch):
        call(engine, "run-1", root_entity_project_id=PUBLIC_PROJECT_ID)

        body = detail(api, monkeypatch)

        assert (body["entity_project_id"], body["kind"]) == (PUBLIC_PROJECT_ID, "agent")

    def test_an_agent_core_cannot_find_has_no_kind(self, api, engine, core, monkeypatch):
        call(engine, "run-1")
        core["applications"] = {}

        body = detail(api, monkeypatch)

        assert (body["entity_project_id"], body["kind"]) == (PROJECT_ID, None)


class TestRecentRuns:
    def test_a_row_carries_every_model_the_version_and_the_conversation(self, api, engine, core, monkeypatch):
        call(engine, "run-1", conversation_id=CONVERSATION)
        call(engine, "run-1", seconds=1, model_name="gpt-b", conversation_id=CONVERSATION, is_error=True)
        call(engine, "run-1", seconds=2, model_name="gpt-b", conversation_id=CONVERSATION)
        call(engine, "run-api", seconds=10, root_entity_version_id=32, entity_name="reviewer (old)",
             conversation_id="00000000-0000-0000-0000-00000000dead")

        body = run_page(api, monkeypatch)

        assert body["total"] == 2
        newest, oldest = body["rows"]
        assert (newest["run_id"], newest["version_name"], newest["conversation_id"], newest["is_error"]) == (
            "run-api", "old", None, False,
        )
        assert oldest["models"] == [
            {"model_name": "gpt-b", "display_name": "Alpha B"}, {"model_name": "gpt-a", "display_name": "GPT A"},
        ]
        assert (oldest["conversation_id"], oldest["conversation_uuid"], oldest["is_error"]) == (
            1701, CONVERSATION, True,
        )
        assert (oldest["total_tokens"], oldest["cost"], oldest["user_email"]) == (330, 0.000003, "ann@example.com")
        assert [c for c in core["calls"] if "conversation_uuids" in c] == [{"skill_refs": [], "conversation_uuids": sorted([
            CONVERSATION, "00000000-0000-0000-0000-00000000dead",
        ])}]
        json.dumps(body)

    def test_a_run_without_a_version_has_no_version_name(self, api, engine, monkeypatch):
        call(engine, "run-1", root_entity_version_id=None)

        row = run_page(api, monkeypatch)["rows"][0]

        assert (row["version_id"], row["version_name"]) == (None, None)

    def test_only_this_agents_human_non_evaluation_runs_are_listed(self, api, engine, monkeypatch):
        mixed_usage(engine)
        call(engine, None, seconds=70)

        body = run_page(api, monkeypatch)

        assert body["total"] == 4
        assert {r["run_id"] for r in body["rows"]} == {"run-manual-1", "run-manual-2", "run-scheduled", "run-webhook"}

    def test_a_failed_conversation_lookup_leaves_ids_null(self, api, engine, core, monkeypatch):
        call(engine, "run-1", conversation_id=CONVERSATION)
        core["down"] = True

        body = run_page(api, monkeypatch)

        assert [(r["run_id"], r["conversation_id"], r["conversation_uuid"]) for r in body["rows"]] == [
            ("run-1", None, CONVERSATION),
        ]


class TestRecentRunsPaging:
    def seed(self, engine):
        for seconds, run_id, tokens in ((0, "run-c", 100), (1, "run-b", 100), (2, "run-a", 300),
                                        (3, "run-e", 200), (4, "run-d", 500)):
            call(engine, run_id, seconds, billable_input_tokens=tokens, output_tokens=0)

    def test_the_sort_holds_across_pages(self, api, engine, monkeypatch):
        self.seed(engine)

        pages = [
            run_page(api, monkeypatch, sort_by="total_tokens", sort_order="desc", limit="2", offset=str(offset))
            for offset in (0, 2, 4)
        ]

        assert {page["total"] for page in pages} == {5}
        assert [r["run_id"] for page in pages for r in page["rows"]] == ["run-d", "run-a", "run-e", "run-b", "run-c"]

    def test_an_unknown_sort_falls_back_to_newest_first(self, api, engine, monkeypatch):
        self.seed(engine)

        body = run_page(api, monkeypatch, sort_by="entity_name; drop table usage_event")

        assert [r["run_id"] for r in body["rows"]] == ["run-d", "run-e", "run-a", "run-b", "run-c"]

    def test_the_model_sort_follows_display_names(self, api, engine, monkeypatch):
        monkeypatch.setattr(_analytics, "model_display_names", lambda project_id: {
            "aa-gpt4": "ZZ GPT-4", "zz-llama": "AA O'Internal",
        })
        statements = []
        event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        call(engine, "run-gpt", model_name="aa-gpt4")
        call(engine, "run-unmapped", seconds=1, model_name="BB-unmapped")
        call(engine, "run-both", seconds=2, model_name="aa-gpt4")
        call(engine, "run-both", seconds=3, model_name="zz-llama")

        body = run_page(api, monkeypatch, sort_by="model", sort_order="asc")

        assert [(r["run_id"], r["models"][0]["display_name"]) for r in body["rows"]] == [
            ("run-both", "AA O'Internal"), ("run-unmapped", "BB-unmapped"), ("run-gpt", "ZZ GPT-4"),
        ]
        assert not [sql for sql in statements if "O'Internal" in sql or "ZZ GPT-4" in sql]
        count_sql, page_sql = [sql for sql in statements if "GROUP BY usage_event.run_id" in sql][:2]
        assert "CASE usage_event.model_name" not in count_sql
        assert "CASE usage_event.model_name" in page_sql

    def test_status_puts_failed_runs_first(self, api, engine, monkeypatch):
        self.seed(engine)
        call(engine, "run-b", seconds=5, is_error=True)

        body = run_page(api, monkeypatch, sort_by="status", limit="1")

        assert [r["run_id"] for r in body["rows"]] == ["run-b"]

    @pytest.mark.parametrize(("limit", "rows"), [("0", 1), ("500", 100)])
    def test_the_page_size_is_clamped(self, api, engine, monkeypatch, limit, rows):
        for index in range(101):
            call(engine, f"run-{index:03d}", index)

        body = run_page(api, monkeypatch, limit=limit)

        assert (body["total"], len(body["rows"])) == (101, rows)

    def test_a_missing_entity_id_is_rejected(self, api, engine, monkeypatch):
        monkeypatch.setattr(api.analytics_agent_runs, "request", types.SimpleNamespace(args={}))

        assert api.analytics_agent_runs.PromptLibAPI().get(PROJECT_ID) == ({"error": "entity_id is required"}, 400)


class TestRunDetailVersions:
    def test_a_deleted_version_is_named_from_the_stored_run_row(self, api, engine, monkeypatch):
        child_call(engine, RUN_ID, root_entity_version_id=242)
        call(engine, RUN_ID, seconds=1, root_entity_version_id=242, entity_name="reviewer (v3)")

        body = run_detail(api, monkeypatch)

        assert body["root_entity_version_ids"] == [242]
        assert body["root_entity_versions"] == [{"id": 242, "name": "v3"}]

    def test_an_existing_version_takes_the_elitea_core_name(self, api, engine, monkeypatch):
        call(engine, RUN_ID, entity_name="reviewer (base)")

        assert run_detail(api, monkeypatch)["root_entity_versions"] == [{"id": 31, "name": "renamed"}]

    def test_a_version_without_a_run_row_falls_back_to_its_id(self, api, engine, monkeypatch):
        child_call(engine, RUN_ID, root_entity_version_id=242)

        assert run_detail(api, monkeypatch)["root_entity_versions"] == [{"id": 242, "name": "Version #242"}]


def skill_call(engine, run_id, seconds=0, **values):
    call(engine, run_id, seconds, root_entity_type="skill", root_entity_id=SKILL_ID, entity_type="skill",
         entity_id=SKILL_ID, **values)


class TestVersionNamesInContext:
    def test_a_public_agent_ignores_a_colliding_local_version(self, api, engine, monkeypatch):
        call(engine, RUN_ID, root_entity_project_id=PUBLIC_PROJECT_ID, entity_name="reviewer (catalog v2)")

        assert run_detail(api, monkeypatch)["root_entity_versions"] == [{"id": 31, "name": "catalog v2"}]
        assert [v["version_name"] for v in detail(api, monkeypatch)["by_version"]] == ["catalog v2"]
        assert [r["version_name"] for r in run_page(api, monkeypatch)["rows"]] == ["catalog v2"]

    def test_a_local_and_a_public_run_on_the_same_version_id_keep_their_own_names(self, api, engine, monkeypatch):
        call(engine, "run-local")
        call(engine, "run-public", seconds=1, root_entity_project_id=PUBLIC_PROJECT_ID,
             entity_name="reviewer (catalog v2)")

        rows = run_page(api, monkeypatch)["rows"]

        assert [(r["run_id"], r["version_id"], r["version_name"]) for r in rows] == [
            ("run-public", 31, "catalog v2"), ("run-local", 31, "renamed"),
        ]

    def test_a_local_version_of_another_agent_falls_back_to_the_stored_name(self, api, engine, monkeypatch):
        call(engine, RUN_ID, root_entity_version_id=77, entity_name="reviewer (mine)")

        assert run_detail(api, monkeypatch)["root_entity_versions"] == [{"id": 77, "name": "mine"}]

    def test_a_skill_run_takes_the_skill_version_name(self, api, engine, monkeypatch):
        skill_call(engine, RUN_ID, entity_name="summarizer (draft)")

        assert run_detail(api, monkeypatch)["root_entity_versions"] == [{"id": 31, "name": "skill v1"}]

    def test_a_deleted_skill_version_falls_back_to_its_id(self, api, engine, monkeypatch):
        skill_call(engine, RUN_ID, root_entity_version_id=99, entity_name="summarizer (old)")

        assert run_detail(api, monkeypatch)["root_entity_versions"] == [{"id": 99, "name": "Version #99"}]

    def test_a_skill_name_ending_in_parentheses_is_not_a_version(self, api, engine, core, monkeypatch):
        core["skills"] = {}
        skill_call(engine, RUN_ID, entity_name="Foo (beta)")

        assert run_detail(api, monkeypatch)["root_entity_versions"] == [{"id": 31, "name": "Version #31"}]

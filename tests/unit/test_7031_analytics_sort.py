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
MODULES = ("analytics_agents", "analytics_users")
COST_KEYS = ("input_cost", "output_cost", "cache_read_cost", "cache_creation_cost")
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


@pytest.fixture
def api(monkeypatch):
    engine = install_engine(monkeypatch, _analytics)
    monkeypatch.setattr(_analytics, "application_meta", lambda project_id, refs=(): {})
    monkeypatch.setattr(_analytics, "label_users", lambda user_ids, stored=None: {})
    monkeypatch.setattr(_analytics, "outsider_admin_ids", lambda project_id, conditions: set())
    monkeypatch.setattr(_analytics, "day_expr", lambda: func.date(FakeUsageEvent.ts, type_=Date))
    monkeypatch.setattr(_analytics, "parse_date_range", lambda args, run_scope=None: WINDOW)
    modules = types.SimpleNamespace(**{n: load_api_module(monkeypatch, n) for n in MODULES})
    modules.engine = engine
    yield modules
    for n in MODULES:
        sys.modules.pop(f"usage.api.v2.{n}", None)
    drop_engine(engine)


def seed(engine, n=5):
    # entity/user i makes i runs (so the default events sort is 5..1) but carries metric n - i + 1
    # on its first row, so every token/cost key sorts 1..n desc: opposite to the fallback
    for i in range(1, n + 1):
        v = n - i + 1
        for run in range(i):
            m = v if run == 0 else 0
            insert_row(engine, ts=TS, project_id=PROJECT_ID, user_id=i, run_id=f"r{i}-{run}",
                       event_type="llm", root_entity_type="application", root_entity_id=i,
                       root_entity_project_id=PROJECT_ID, entity_type="application", entity_id=i,
                       entity_name=f"a{i}", is_error=False,
                       input_tokens=m, output_tokens=m, cache_read_tokens=m, cache_creation_tokens=m,
                       cost_nano_usd=4 * m * 1_000_000,
                       input_cost_nano_usd=m * 1_000_000, output_cost_nano_usd=m * 1_000_000,
                       cache_read_cost_nano_usd=m * 1_000_000, cache_creation_cost_nano_usd=m * 1_000_000)


def get(api, module, monkeypatch, **args):
    mod = getattr(api, module)
    monkeypatch.setattr(mod, "request", types.SimpleNamespace(args={k: str(v) for k, v in args.items()}))
    body, status = mod.PromptLibAPI().get(PROJECT_ID)
    assert status == 200, body
    return body


@pytest.mark.parametrize("module, id_key", [("analytics_agents", "entity_id"), ("analytics_users", "user_id")])
@pytest.mark.parametrize("key", TOKEN_KEYS + COST_KEYS)
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_new_keys_sort(api, monkeypatch, module, id_key, key, order):
    seed(api.engine)
    rows = get(api, module, monkeypatch, sort_by=key, sort_order=order)["rows"]
    expected = [1, 2, 3, 4, 5] if order == "desc" else [5, 4, 3, 2, 1]
    assert [r[id_key] for r in rows] == expected


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_users_skill_events_sort(api, monkeypatch, order):
    for i, runs in ((1, 1), (2, 3), (3, 2)):
        for run in range(runs):
            insert_row(api.engine, ts=TS, project_id=PROJECT_ID, user_id=i, run_id=f"s{i}-{run}",
                       event_type="llm", root_entity_type="skill", root_entity_id=100,
                       root_entity_project_id=PROJECT_ID, entity_type="skill", entity_id=100,
                       entity_name="sk", is_error=False)
    for i, filler in ((1, 9), (2, 1), (3, 5)):
        for k in range(filler):
            insert_row(api.engine, ts=TS, project_id=PROJECT_ID, user_id=i, run_id=f"t{i}-{k}",
                       event_type="tool", root_entity_type="application", root_entity_id=200,
                       root_entity_project_id=PROJECT_ID, entity_type="tool", entity_id=200,
                       entity_name="t", is_error=False)
    rows = get(api, "analytics_users", monkeypatch, sort_by="skill_events", sort_order=order)["rows"]
    assert [r["user_id"] for r in rows] == ([2, 3, 1] if order == "desc" else [1, 3, 2])


@pytest.mark.parametrize("module, id_key", [("analytics_agents", "entity_id"), ("analytics_users", "user_id")])
def test_ties_page_without_repeats(api, monkeypatch, module, id_key):
    seed(api.engine)
    seen = []
    for offset in range(5):
        seen += [r[id_key] for r in get(api, module, monkeypatch, sort_by="errors", limit=1, offset=offset)["rows"]]
    assert sorted(seen) == [1, 2, 3, 4, 5]

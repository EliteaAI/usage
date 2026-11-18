"""#7581: complexity list search, percentile sort and single-run lookup."""

import types

from fixtures.usage_event_table import load_api_module
from usage.methods import _complexity as cx
from usage.methods import _complexity_analytics as ca

STATE = {"thresholds": cx.DEFAULT_THRESHOLDS}


def handler_for(monkeypatch, name, args, queries, matches=None):
    module = load_api_module(monkeypatch, name)
    module.request.args = args
    monkeypatch.setattr(module.an, "fetch_one", lambda statement: STATE)
    monkeypatch.setattr(module.an, "fetch_all", lambda statement: queries.append(statement) or [])
    monkeypatch.setattr(module.an, "application_search", lambda project_id, search: matches or [])
    handler = module.PromptLibAPI()
    handler.module = types.SimpleNamespace(usage_complexity_is_enabled=lambda: True)
    return handler


class TestPercentileSort:
    def test_percentile_ranks_by_median_with_unlabelled_rows_last_both_ways(self):
        for direction in ("asc", "desc"):
            clause = ca.order_clause(ca.ENTITY_SORTS, "percentile", direction, "cu_median")
            assert f"a.runs >= {cx.ENTITY_LABEL_MIN_RUNS} THEN m.cu_median" in clause
            assert clause.endswith("NULLS LAST")

    def test_percentile_sort_compiles_into_the_list_query(self):
        order = ca.order_clause(ca.ENTITY_SORTS, "percentile", "asc", "cu_median")
        assert "ORDER BY (CASE WHEN a.runs" in str(ca.entity_statement(order))


class TestSearch:
    def test_search_filter_is_off_by_default(self):
        assert "search_entity_ids" not in str(ca.entity_statement("runs DESC"))

    def test_search_filters_by_project_and_id_pairs(self):
        assert ":search_project_ids" in str(ca.entity_statement("runs DESC", search=True))
        params = ca.search_params([{"project_id": 3, "id": 7}, {"project_id": 1, "id": 9}])
        assert params == {"search_project_ids": [3, 1], "search_entity_ids": [7, 9]}

    def test_no_name_match_returns_empty_without_querying(self, monkeypatch):
        queries = []
        body, status = handler_for(monkeypatch, "analytics_complexity", {"search": "nope"}, queries).get(3)
        assert status == 200 and body["total"] == 0 and body["rows"] == [] and not queries

    def test_a_match_restricts_the_list_query(self, monkeypatch):
        queries = []
        handler = handler_for(
            monkeypatch, "analytics_complexity", {"search": "bot"}, queries, matches=[{"project_id": 3, "id": 7}],
        )
        assert handler.get(3)[1] == 200
        assert len(queries) == 1 and ":search_entity_ids" in str(queries[0])


class TestRunLookup:
    def test_run_id_filter_only_when_asked(self):
        statement, params = ca.runs_statement("ts DESC", run_id="r1")
        assert "(run_id = :run_id OR conversation_id = :run_id)" in str(statement) and params == {"run_id": "r1"}
        assert "run_id = :run_id" not in str(ca.runs_statement("ts DESC")[0])

    def test_run_lookup_reads_only_the_run_scoped_to_the_entity(self, monkeypatch):
        queries = []
        args = {"entity_id": "7", "run_id": "r1"}
        body, status = handler_for(monkeypatch, "analytics_complexity_detail", args, queries).get(3)
        assert status == 200 and len(queries) == 1
        sql = str(queries[0])
        assert "run_id = :run_id" in sql and "root_entity_id = :entity_id" in sql and "project_id = :project_id" in sql
        assert body["summary"] is None and body["runs"] == []

    def test_run_lookup_without_dates_is_not_limited_to_the_default_week(self, monkeypatch):
        queries = []
        handler_for(monkeypatch, "analytics_complexity_detail", {"entity_id": "7", "run_id": "r1"}, queries).get(3)
        dt_from = queries[0].compile().params["dt_from"]
        assert dt_from.year == 2000

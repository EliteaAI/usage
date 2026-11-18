"""#7581: complexity list search, percentile sort and single-run lookup."""

import types

from fixtures.usage_event_table import load_api_module
from usage.methods import _complexity as cx
from usage.methods import _complexity_analytics as ca

STATE = {"thresholds": cx.DEFAULT_THRESHOLDS}


def handler_for(monkeypatch, name, args, queries, matches=None, truncated=False):
    module = load_api_module(monkeypatch, name)
    module.request.args = args
    monkeypatch.setattr(module.an, "fetch_one", lambda statement: STATE)
    monkeypatch.setattr(module.an, "fetch_all", lambda statement: queries.append(statement) or [])
    monkeypatch.setattr(module.an, "application_search", lambda project_id, search: (matches or [], truncated))
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
        body, status = handler.get(3)
        assert status == 200 and body["search_truncated"] is False
        assert len(queries) == 1 and ":search_entity_ids" in str(queries[0])

    def test_a_truncated_name_lookup_is_reported(self, monkeypatch):
        queries = []
        handler = handler_for(
            monkeypatch, "analytics_complexity", {"search": "a"}, queries,
            matches=[{"project_id": 3, "id": 7}], truncated=True,
        )
        assert handler.get(3)[0]["search_truncated"] is True


class TestRunLookup:
    # Cross-review: a conversation uuid resolves to the run Run History's CU column shows
    def test_a_conversation_lookup_picks_its_most_complex_run_like_run_history(self):
        statement, _ = ca.runs_statement("ts DESC", run_id="conv-1")
        assert "DISTINCT ON (conversation_id)" in str(statement) and "cu_total DESC" in str(statement)

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


class TestSearchFailure:
    """Cross-review: an elitea_core outage must not look like "no agents match"."""

    def rpc(self, monkeypatch, behaviour):
        import sys
        tools = sys.modules["tools"]
        call = types.SimpleNamespace(elitea_core_usage_entity_meta=behaviour)
        rpc = types.SimpleNamespace(timeout=lambda seconds: call)
        monkeypatch.setattr(tools, "rpc_tools", types.SimpleNamespace(RpcMixin=lambda: types.SimpleNamespace(rpc=rpc)), raising=False)

    def test_a_failed_lookup_raises(self, monkeypatch):
        from usage.methods import _analytics as an

        def boom(*args, **kwargs):
            raise TimeoutError("rpc timeout")

        self.rpc(monkeypatch, boom)
        import pytest
        with pytest.raises(an.ApplicationSearchUnavailable):
            an.application_search(3, "bot")

    def test_an_elitea_core_without_search_support_raises(self, monkeypatch):
        from usage.methods import _analytics as an
        self.rpc(monkeypatch, lambda *a, **k: {"applications": []})
        import pytest
        with pytest.raises(an.ApplicationSearchUnavailable):
            an.application_search(3, "bot")

    def test_a_real_empty_match_is_not_an_error(self, monkeypatch):
        from usage.methods import _analytics as an
        self.rpc(monkeypatch, lambda *a, **k: {"matched_applications": [], "matched_applications_truncated": False})
        assert an.application_search(3, "bot") == ([], False)

    def test_the_list_returns_503_instead_of_an_empty_200(self, monkeypatch):
        module = load_api_module(monkeypatch, "analytics_complexity")
        module.request.args = {"search": "bot"}

        def unavailable(project_id, search):
            raise module.an.ApplicationSearchUnavailable("down")

        monkeypatch.setattr(module.an, "fetch_one", lambda statement: STATE)
        monkeypatch.setattr(module.an, "application_search", unavailable)
        handler = module.PromptLibAPI()
        handler.module = types.SimpleNamespace(usage_complexity_is_enabled=lambda: True)
        assert handler.get(3)[1] == 503

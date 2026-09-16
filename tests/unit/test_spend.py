"""Spend aggregations — the three-way distinction between data, no data, and a failed read.

`available` says whether the query succeeded, not whether the project spent anything: a quiet
project must render a clean $0.00 with no "still being collected" banner, which is why the
zero-rows case and the raising case are asserted separately everywhere below.
"""
import datetime
import re

import pytest

from fixtures.helpers import bind, fake_module
from usage.methods import spend

PERIOD = "202609"
DAY = datetime.datetime(2026, 9, 11, tzinfo=datetime.timezone.utc)


class Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class Connection:
    """Returns the canned result sets in the order the code executes them."""

    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    def execute(self, statement, params=None):  # pylint: disable=W0613
        self.statements.append(str(statement))
        #
        return Result(self.results.pop(0) if self.results else [])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Engine:
    def __init__(self, *results):
        self.connection = Connection(results)

    def connect(self):
        return self.connection


class FailingEngine:
    def connect(self):
        raise RuntimeError("no database")


@pytest.fixture
def build(monkeypatch):
    """A bound Module stand-in whose db.engine is whatever the test hands over."""
    def make(engine):
        monkeypatch.setattr(spend.db, "engine", engine, raising=False)
        #
        return bind(fake_module(), spend.Method), engine
    #
    return make


# (input, output, cost_micro, calls) as the counter select projects it
COUNTER_ROW = (120, 40, 1_500_000, 2)


class TestProjectSpend:
    def test_a_counter_row_populates_the_spend_shape(self, build):
        instance, _ = build(Engine([COUNTER_ROW]))
        #
        result = instance.usage_read_project_spend(project_id=7, period=PERIOD)
        #
        assert result["spend"] == 1.5
        assert isinstance(result["spend"], float)
        assert result["prompt_tokens"] == 120
        assert result["completion_tokens"] == 40
        assert result["total_tokens"] == 160
        assert result["available"] is True
        assert result["period"] == PERIOD

    def test_no_rows_is_zero_but_still_available(self, build):
        instance, _ = build(Engine([]))
        #
        result = instance.usage_read_project_spend(project_id=7, period=PERIOD)
        #
        assert result["spend"] == 0.0
        assert result["available"] is True

    def test_a_failed_read_is_the_unavailable_answer(self, build):
        instance, _ = build(FailingEngine())
        #
        result = instance.usage_read_project_spend(project_id=7, period=PERIOD)
        #
        assert result["available"] is False
        assert result["tag"] == f"project-7-{PERIOD}"

    def test_no_period_resolves_to_the_current_utc_month(self, build):
        instance, _ = build(Engine([COUNTER_ROW]))
        #
        assert instance.usage_read_project_spend(project_id=7)["period"] == spend.current_period()

    def test_only_the_project_aggregate_row_is_read(self, build):
        instance, engine = build(Engine([COUNTER_ROW]))
        #
        instance.usage_read_project_spend(project_id=7, period=PERIOD)
        #
        assert "user_id" in engine.connection.statements[0]


class TestUserSpend:
    def test_the_member_row_populates_the_shape(self, build):
        instance, _ = build(Engine([COUNTER_ROW]))
        #
        result = instance.usage_read_user_spend(project_id=7, user_id=42, period=PERIOD)
        #
        assert result["spend"] == 1.5
        assert result["tag"] == f"user-42-{PERIOD}"
        assert result["available"] is True

    def test_a_failed_read_is_unavailable(self, build):
        instance, _ = build(FailingEngine())
        #
        result = instance.usage_read_user_spend(project_id=7, user_id=42, period=PERIOD)
        #
        assert result["available"] is False


class TestMapReads:
    def test_every_requested_project_is_keyed_even_without_data(self, build):
        instance, _ = build(Engine([(1, 2_000_000)]))
        #
        spend_map = instance.usage_read_projects_spend(project_ids=[1, 2], period=PERIOD)
        #
        assert spend_map == {1: 2.0, 2: 0.0}

    def test_every_requested_member_is_keyed_even_without_data(self, build):
        instance, _ = build(Engine([(4, 500_000)]))
        #
        spend_map = instance.usage_read_users_spend(
            project_id=7, user_ids=[4, 5], period=PERIOD,
        )
        #
        assert spend_map == {4: 0.5, 5: 0.0}

    def test_a_failed_read_still_returns_a_full_zero_map(self, build):
        instance, _ = build(FailingEngine())
        #
        assert instance.usage_read_projects_spend(project_ids=[1, 2]) == {1: 0.0, 2: 0.0}

    def test_ids_are_chunked_so_an_in_list_cannot_grow_unbounded(self):
        chunks = spend._chunks(range(spend.ID_CHUNK + 5))  # pylint: disable=W0212
        #
        assert [len(chunk) for chunk in chunks] == [spend.ID_CHUNK, 5]


class TestMemberSpendListing:
    def test_the_sentinel_row_becomes_the_project_total(self, build):
        # (user_id, cost_micro, call_count); user_id 0 is the project aggregate
        instance, _ = build(Engine([(0, 3_000_000, 10), (42, 1_000_000, 4)]))
        #
        result = instance.usage_read_member_spend_listing(project_id=7, period=PERIOD)
        #
        assert result["project"]["spend"] == 3.0
        assert result["members"] == {42: {"spend": 1.0, "requests": 4}}

    def test_no_rows_is_an_empty_listing_not_none(self, build):
        instance, _ = build(Engine([]))
        #
        assert instance.usage_read_member_spend_listing(project_id=7) == {
            "project": {"spend": 0.0}, "members": {},
        }

    def test_a_failed_read_keeps_the_none_unreachable_contract(self, build):
        # elitea_core/api/v2/user_budgets.py branches on None to fall back
        instance, _ = build(FailingEngine())
        #
        assert instance.usage_read_member_spend_listing(project_id=7) is None


# totals, then per-model rows, then per-day rows
DETAIL_RESULTS = (
    [(120, 40, 5, 7, 1_500_000, 2)],
    [("gpt-4o", 1_500_000, 160, 2)],
    [(DAY, 1_500_000, 160, 2)],
)


class TestUsageDetail:
    def test_totals_models_and_daily_are_all_filled(self, build):
        instance, _ = build(Engine(*DETAIL_RESULTS))
        #
        result = instance.usage_read_project_usage_detail(project_id=7, period=PERIOD)
        #
        assert result["spend"] == 1.5
        assert result["input_tokens"] == 120
        assert result["output_tokens"] == 40
        assert result["total_tokens"] == 160
        assert result["cache_read_tokens"] == 5
        assert result["cache_creation_tokens"] == 7
        assert result["api_requests"] == 2
        assert result["models"] == [
            {"model": "gpt-4o", "spend": 1.5, "total_tokens": 160, "api_requests": 2},
        ]
        # api_requests is what the chart's own emptiness check reads, so it has to be here
        assert result["daily"] == [
            {"date": "2026-09-11", "spend": 1.5, "total_tokens": 160, "api_requests": 2},
        ]
        assert result["available"] is True

    def test_the_model_rows_are_ordered_by_spend(self, build):
        # The table numbers its rows and draws share bars straight from payload order, so an
        # unsorted group-by buried the dominant model halfway down the list
        instance, engine = build(Engine(*DETAIL_RESULTS))
        #
        instance.usage_read_project_usage_detail(project_id=7, period=PERIOD)
        #
        assert "ORDER BY" in str(engine.connection.statements[1]).upper()

    def test_an_untouched_project_is_empty_but_available(self, build):
        instance, _ = build(Engine([], [], []))
        #
        result = instance.usage_read_project_usage_detail(project_id=7, period=PERIOD)
        #
        assert result["models"] == []
        assert result["daily"] == []
        assert result["spend"] == 0.0
        assert result["available"] is True

    def test_a_failed_read_is_unavailable(self, build):
        instance, _ = build(FailingEngine())
        #
        assert instance.usage_read_project_usage_detail(project_id=7)["available"] is False

    def test_the_member_variant_tags_and_filters_by_user(self, build):
        instance, engine = build(Engine(*DETAIL_RESULTS))
        #
        result = instance.usage_read_user_usage_detail(
            project_id=7, user_id=42, period=PERIOD,
        )
        #
        assert result["tag"] == f"user-42-{PERIOD}"
        assert "user_id" in engine.connection.statements[0]

    def test_only_llm_events_are_summed(self, build):
        instance, engine = build(Engine(*DETAIL_RESULTS))
        #
        instance.usage_read_project_usage_detail(project_id=7, period=PERIOD)
        #
        for statement in engine.connection.statements:
            assert "event_type" in statement

    def test_only_the_intended_columns_are_filtered_on(self, build):
        # A whitelist, not an "is_error absent" blacklist: a failed call still cost money, and
        # any other unintended predicate would silently drop rows from the page total too
        instance, engine = build(Engine(*DETAIL_RESULTS))
        #
        instance.usage_read_project_usage_detail(project_id=7, period=PERIOD)
        #
        for statement in engine.connection.statements:
            where = re.split(r"GROUP BY|ORDER BY", statement.split("WHERE", 1)[1])[0]
            assert set(re.findall(r"usage_event\.(\w+)", where)) == {
                "project_id", "ts", "event_type",
            }

    def test_the_member_variant_filters_on_user_id_and_nothing_more(self, build):
        instance, engine = build(Engine(*DETAIL_RESULTS))
        #
        instance.usage_read_user_usage_detail(project_id=7, user_id=42, period=PERIOD)
        #
        for statement in engine.connection.statements:
            where = re.split(r"GROUP BY|ORDER BY", statement.split("WHERE", 1)[1])[0]
            assert set(re.findall(r"usage_event\.(\w+)", where)) == {
                "project_id", "ts", "event_type", "user_id",
            }

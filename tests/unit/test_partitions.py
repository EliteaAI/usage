"""Month arithmetic and partition DDL generation. No database involved."""
from datetime import date

import pytest

from usage.methods import mode as mode_module
from usage.methods import partitions

from fixtures.helpers import bind, fake_module
from fixtures.stubs import RecordingEngine


class TestMonthArithmetic:
    def test_next_month_within_year(self):
        assert partitions.next_month(2026, 9) == (2026, 10)

    def test_december_rolls_the_year(self):
        assert partitions.next_month(2026, 12) == (2027, 1)

    def test_partition_name_is_zero_padded(self):
        assert partitions.partition_name(2026, 9) == "usage_event_202609"
        assert partitions.partition_name(2026, 12) == "usage_event_202612"

    def test_bounds_are_half_open(self):
        # An inclusive upper bound would put the 1st of each month in two partitions.
        assert partitions.month_bounds(2026, 9) == (date(2026, 9, 1), date(2026, 10, 1))

    def test_december_bounds_cross_the_year(self):
        assert partitions.month_bounds(2026, 12) == (date(2026, 12, 1), date(2027, 1, 1))


class TestMonthRange:
    def test_inclusive_at_both_ends(self):
        assert partitions.month_range((2026, 9), (2026, 11)) == [(2026, 9), (2026, 10), (2026, 11)]

    def test_single_month(self):
        assert partitions.month_range((2026, 9), (2026, 9)) == [(2026, 9)]

    def test_empty_when_to_is_before_from(self):
        assert partitions.month_range((2026, 9), (2026, 8)) == []

    def test_spans_a_year_boundary(self):
        assert partitions.month_range((2026, 12), (2027, 1)) == [(2026, 12), (2027, 1)]


class TestStatements:
    def test_one_statement_per_month_in_order(self):
        statements = partitions.partition_statements("centry", (2026, 11), (2027, 1))
        #
        assert len(statements) == 3
        assert "usage_event_202611" in statements[0]
        assert "usage_event_202612" in statements[1]
        assert "usage_event_202701" in statements[2]

    def test_statement_is_idempotent_and_schema_qualified(self):
        statement = partitions.partition_statements("centry", (2026, 9), (2026, 9))[0]
        #
        assert "CREATE TABLE IF NOT EXISTS centry.usage_event_202609" in statement
        assert "PARTITION OF centry.usage_event" in statement
        assert "FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')" in statement

    def test_no_statements_for_an_inverted_range(self):
        assert partitions.partition_statements("centry", (2026, 9), (2026, 8)) == []


class TestEnsurePartitions:
    def instance(self, config, engine):
        instance = bind(
            fake_module(config={"usage": config}), mode_module.Method, partitions.Method,
        )
        partitions.db.engine = engine
        #
        return instance

    def test_lookahead_creates_current_plus_ahead(self, monkeypatch):
        engine = RecordingEngine()
        instance = self.instance({"partition_ahead_months": 2}, engine)
        #
        count = instance.usage_ensure_partitions(from_month=(2026, 9))
        #
        assert count == 3
        assert engine.connection.commits == 1
        assert len(engine.connection.statements) == 3

    def test_zero_lookahead_still_creates_the_current_month(self):
        engine = RecordingEngine()
        instance = self.instance({"partition_ahead_months": 0}, engine)
        #
        assert instance.usage_ensure_partitions(from_month=(2026, 9)) == 1

    def test_explicit_range_wins_over_config(self):
        engine = RecordingEngine()
        instance = self.instance({"partition_ahead_months": 5}, engine)
        #
        assert instance.usage_ensure_partitions(
            from_month=(2026, 9), to_month=(2026, 9),
        ) == 1

    def test_defaults_to_today_when_no_range_given(self):
        engine = RecordingEngine()
        instance = self.instance({"partition_ahead_months": 1}, engine)
        #
        assert instance.usage_ensure_partitions() == 2
        assert partitions.partition_name(date.today().year, date.today().month) \
            in engine.connection.statements[0]

    def test_no_default_partition_is_ever_created(self):
        engine = RecordingEngine()
        instance = self.instance({}, engine)
        instance.usage_ensure_partitions()
        #
        assert not any("DEFAULT" in statement for statement in engine.connection.statements)


class TestParentGuard:
    """With apply_shared_metadata off, the parent only appears once an operator runs the
    admin create_tables task — until then this must warn, not raise."""

    def instance(self, engine):
        instance = bind(
            fake_module(config={"usage": {}}), mode_module.Method, partitions.Method,
        )
        partitions.db.engine = engine
        #
        return instance

    def test_no_partitions_created_when_the_parent_is_absent(self, recording_log):
        engine = RecordingEngine(parent_exists=False)
        instance = self.instance(engine)
        #
        assert instance.usage_ensure_partitions() == 0
        assert engine.connection.statements == []
        assert engine.connection.commits == 0
        assert any(
            "run the admin create_tables task" in message
            for message in recording_log.messages("warning")
        )

    def test_the_probe_asks_for_a_partitioned_relation(self):
        engine = RecordingEngine(parent_exists=False)
        self.instance(engine).usage_ensure_partitions()
        #
        statement, params = engine.connection.queries[0]
        #
        assert "relkind = ANY(:relkinds)" in statement
        assert params["schema"] == "centry"
        assert params["relkinds"] == ["p"]

    def test_partitions_are_created_once_the_parent_exists(self):
        engine = RecordingEngine(parent_exists=True)
        #
        assert self.instance(engine).usage_ensure_partitions(from_month=(2026, 9)) >= 1
        assert engine.connection.commits == 1


class TestUsagePartitionExists:
    def instance(self, engine):
        instance = bind(
            fake_module(config={"usage": {}}), mode_module.Method, partitions.Method,
        )
        partitions.db.engine = engine
        #
        return instance

    def test_defaults_to_the_current_month(self):
        engine = RecordingEngine(parent_exists=True)
        self.instance(engine).usage_partition_exists()
        #
        statement, params = engine.connection.queries[0]
        #
        assert params["relname"] == partitions.partition_name(
            date.today().year, date.today().month,
        )
        assert params["relkinds"] == ["r"]

    def test_true_when_the_child_partition_is_present(self):
        engine = RecordingEngine(parent_exists=True)
        assert self.instance(engine).usage_partition_exists(2026, 9) is True

    def test_false_when_the_child_partition_is_missing(self):
        engine = RecordingEngine(parent_exists=False)
        assert self.instance(engine).usage_partition_exists(2026, 9) is False

    def test_december_rolls_into_the_next_year_name(self):
        engine = RecordingEngine(parent_exists=True)
        self.instance(engine).usage_partition_exists(2026, 12)
        #
        _, params = engine.connection.queries[0]
        assert params["relname"] == "usage_event_202612"

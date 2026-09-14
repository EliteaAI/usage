"""Reconcile — the drift report, and the helpers it reaches for.

These call through a bound Module rather than the resource class, because the bug this file
was added for was invisible from the class: a plain staticmethod is never bound onto the
Module, so `self._accumulate(...)` raised AttributeError only once the cron fired.
"""
import datetime

from fixtures.helpers import bind, fake_module
from usage.methods import reconcile

START = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
END = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)


class Rows:
    """A connection returning fixed aggregate tuples, shaped as the SELECT projects them."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return iter(self.rows)


def build():
    return bind(fake_module(), reconcile.Method)


class TestFactTotals:
    def test_a_project_row_and_a_member_row_are_produced_per_fact(self):
        # (project_id, user_id, input, output, cost, calls)
        connection = Rows([(42, 7, 10, 20, 1000, 1)])
        #
        totals = build().usage_fact_totals(connection, START, END)
        #
        assert totals[(42, reconcile.PROJECT_USER_SENTINEL)]["cost_micro_usd"] == 1000
        assert totals[(42, 7)]["cost_micro_usd"] == 1000

    def test_two_members_sum_into_one_project_total(self):
        connection = Rows([(42, 7, 10, 20, 1000, 1), (42, 8, 5, 5, 250, 2)])
        #
        totals = build().usage_fact_totals(connection, START, END)
        #
        project = totals[(42, reconcile.PROJECT_USER_SENTINEL)]
        assert project["cost_micro_usd"] == 1250
        assert project["call_count"] == 3

    def test_a_row_without_a_user_counts_only_towards_the_project(self):
        connection = Rows([(42, None, 10, 20, 1000, 1)])
        #
        totals = build().usage_fact_totals(connection, START, END)
        #
        assert list(totals) == [(42, reconcile.PROJECT_USER_SENTINEL)]


class TestRepairRow:
    def test_the_repair_is_a_difference_because_the_upsert_accumulates(self):
        row = {
            "project_id": 42, "user_id": 7, "period_start": START.date(),
            "expected": {"input_tokens": 10, "output_tokens": 20,
                         "cost_micro_usd": 1000, "call_count": 3},
            "actual": {"input_tokens": 4, "output_tokens": 5,
                       "cost_micro_usd": 400, "call_count": 1},
        }
        #
        repair = reconcile._repair_row(row)  # pylint: disable=W0212
        #
        assert repair["cost_micro_usd"] == 600
        assert repair["call_count"] == 2
        assert repair["model_name"] == reconcile.ALL_MODELS_SENTINEL

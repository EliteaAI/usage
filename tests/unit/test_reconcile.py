"""Reconcile — the drift report, and the helpers it reaches for.

These call through a bound Module rather than the resource class, because the bug this file
was added for was invisible from the class: a plain staticmethod is never bound onto the
Module, so `self._accumulate(...)` raised AttributeError only once the cron fired.
"""
import datetime
import types

import pytest

from fixtures.fake_redis import RecordingRedis
from fixtures.helpers import bind, fake_module
from usage.methods import reconcile

START = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
END = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)


class Rows:
    """A connection returning fixed aggregate tuples, shaped as the SELECT projects them."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, statement=None, *_args, **_kwargs):
        self.statements.append(statement)
        #
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

    def test_only_llm_facts_are_summed(self):
        # Nothing counts other event types into usage_counter, so summing them here would
        # report drift the drainer can never close
        connection = Rows([])
        #
        build().usage_fact_totals(connection, START, END)
        #
        assert "event_type" in str(connection.statements[0])

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


class TestDrift:
    """Both sides are scanned, because a counter can be wrong in either direction."""

    def _instance(self, facts, counters):
        instance = build()
        instance.usage_fact_totals = lambda *_a, **_k: facts
        instance.usage_counter_totals = lambda *_a, **_k: counters
        #
        return instance

    def _totals(self, cost):
        return {"input_tokens": 0, "output_tokens": 0, "cost_micro_usd": cost, "call_count": 1}

    def test_agreement_is_not_drift(self):
        totals = {(42, 7): self._totals(1000)}
        #
        assert self._instance(totals, dict(totals)).usage_counter_drift(None, START, END) == []

    def test_a_counter_row_with_no_facts_behind_it_is_drift(self):
        # Left out of the scan this row stays inflated forever: iterating the facts alone
        # never visits a key that only the counters have
        instance = self._instance({}, {(42, 7): self._totals(1000)})
        #
        drift = instance.usage_counter_drift(None, START, END)
        #
        assert len(drift) == 1
        assert drift[0]["expected"]["cost_micro_usd"] == 0
        assert drift[0]["actual"]["cost_micro_usd"] == 1000

    def test_an_inflated_counter_repairs_by_a_negative_delta(self):
        instance = self._instance({(42, 7): self._totals(400)}, {(42, 7): self._totals(1000)})
        #
        repair = reconcile._repair_row(  # pylint: disable=W0212
            instance.usage_counter_drift(None, START, END)[0],
        )
        #
        assert repair["cost_micro_usd"] == -600

    def test_a_missing_counter_row_is_still_drift(self):
        instance = self._instance({(42, 7): self._totals(1000)}, {})
        #
        assert instance.usage_counter_drift(None, START, END)[0]["actual"]["cost_micro_usd"] == 0


class FakeLockConnection:
    """Stands in for a live connection asked for the advisory lock's scalar result."""

    def __init__(self, acquired=True):
        self.acquired = acquired

    def execute(self, _statement):
        return types.SimpleNamespace(scalar=lambda: self.acquired)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class BrokenConnection:
    """A connection whose every execute() raises, as if postgres were unreachable."""

    def execute(self, _statement):
        raise RuntimeError("connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestReconcileLock:
    def test_lock_succeeds_when_postgres_grants_it(self):
        assert build().usage_reconcile_lock(FakeLockConnection(acquired=True)) is True

    def test_lock_fails_when_postgres_refuses_it(self):
        assert build().usage_reconcile_lock(FakeLockConnection(acquired=False)) is False

    def test_a_lock_attempt_on_an_unreachable_connection_is_logged_not_raised(self):
        # An exception here must never propagate: it would skip straight past the "another
        # run holds the lock" skip-path and repair unlocked
        assert build().usage_reconcile_lock(BrokenConnection()) is False

    def test_unlock_is_best_effort_on_a_broken_connection(self):
        build().usage_reconcile_unlock(BrokenConnection())  # must not raise


def patch_engine(monkeypatch, lock_acquired=True):
    """The first connect() is the reconciler's own lock attempt; every later one (opened
    inside _reconcile_apply to compute drift) is a plain, already-successful connection."""
    calls = []
    #
    def connect():
        calls.append(None)
        return FakeLockConnection(acquired=lock_acquired if len(calls) == 1 else True)
    #
    monkeypatch.setattr(reconcile.db, "engine", types.SimpleNamespace(connect=connect), raising=False)
    #
    return calls


class TestReconcileCountersConcurrency:
    """Acceptance criterion 2: a second concurrent apply run must not repair alongside the first."""

    def test_a_report_only_run_never_touches_the_lock(self, monkeypatch):
        instance = build()
        calls = patch_engine(monkeypatch, lock_acquired=True)
        instance.usage_counter_drift = lambda *_a, **_k: []
        #
        result = instance.usage_reconcile_counters(period="202609", apply=False)
        #
        assert result["applied"] is False
        assert calls == [None]  # only the drift-read connection, never the lock

    def test_apply_runs_when_it_wins_the_lock(self, monkeypatch):
        instance = build()
        patch_engine(monkeypatch, lock_acquired=True)
        instance.usage_counter_drift = lambda *_a, **_k: []
        instance.usage_reconcile_record_run = lambda _summary: None
        #
        result = instance.usage_reconcile_counters(period="202609", apply=True)
        #
        assert result["applied"] is True
        assert result.get("skipped") is None

    def test_a_concurrent_apply_is_skipped_not_queued_or_retried(self, monkeypatch, recording_log):
        instance = build()
        patch_engine(monkeypatch, lock_acquired=False)
        #
        result = instance.usage_reconcile_counters(period="202609", apply=True)
        #
        assert result == {"period": "202609", "applied": False, "drift": None, "skipped": "locked"}
        assert any("skipped" in message for message in recording_log.messages("warning"))

    def test_the_lock_is_released_even_when_repair_raises(self, monkeypatch):
        instance = build()
        patch_engine(monkeypatch, lock_acquired=True)
        instance.usage_counter_drift = lambda *_a, **_k: [{"project_id": 1}]
        instance.usage_reconcile_repair = \
            lambda _drift: (_ for _ in ()).throw(RuntimeError("boom"))
        released = []
        original_unlock = instance.usage_reconcile_unlock
        instance.usage_reconcile_unlock = \
            lambda connection: (released.append(True), original_unlock(connection))[0]
        #
        with pytest.raises(RuntimeError):
            instance.usage_reconcile_counters(period="202609", apply=True)
        #
        assert released == [True]


def drift_row(project_id, user_id=7, cost=100):
    return {
        "project_id": project_id, "user_id": user_id, "period_start": START.date(),
        "expected": {"input_tokens": 0, "output_tokens": 0, "cost_micro_usd": cost, "call_count": 1},
        "actual": {"input_tokens": 0, "output_tokens": 0, "cost_micro_usd": 0, "call_count": 0},
    }


class RepairConnection:
    """Records the delta each counter_upsert() call carries; raises for poisoned rows."""

    def __init__(self, poison_project_ids=()):
        self.poison = set(poison_project_ids)
        self.applied = []
        self.commits = 0

    def execute(self, delta):
        if delta["project_id"] in self.poison:
            raise RuntimeError("boom")
        #
        self.applied.append(delta)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def patch_repair_engine(monkeypatch, instance, poison_project_ids=()):
    """counter_upsert is faked to pass the delta straight through, so RepairConnection can
    inspect it without a real dialect — one fresh connection per batch, all sharing the same
    poison set, since a row must stay poisoned across a retry in a brand new connection."""
    created = []
    #
    def connect():
        connection = RepairConnection(poison_project_ids)
        created.append(connection)
        #
        return connection
    #
    monkeypatch.setattr(reconcile.db, "engine", types.SimpleNamespace(connect=connect), raising=False)
    monkeypatch.setattr(reconcile, "counter_upsert", lambda delta: delta)
    instance.usage_config = getattr(instance, "usage_config", lambda: {})
    #
    return created


class TestReconcileRepairBatching:
    """Acceptance criterion 3: a failed repair is loud, not deduped, and never partially written."""

    def test_all_rows_repair_in_one_batch_under_the_batch_size(self, monkeypatch):
        instance = build()
        connections = patch_repair_engine(monkeypatch, instance)
        #
        repaired, failed = instance.usage_reconcile_repair([drift_row(1), drift_row(2)])
        #
        assert repaired == 2
        assert failed == []
        assert len(connections) == 1
        assert connections[0].commits == 1

    def test_the_batch_size_is_configurable(self, monkeypatch):
        instance = build()
        instance.usage_config = lambda: {"reconcile": {"repair_batch_size": 1}}
        connections = patch_repair_engine(monkeypatch, instance)
        #
        repaired, _failed = instance.usage_reconcile_repair(
            [drift_row(1), drift_row(2), drift_row(3)],
        )
        #
        assert repaired == 3
        assert len(connections) == 3  # one connection per batch, at batch size 1

    def test_a_poisoned_row_is_isolated_not_lost_with_the_rest_of_its_batch(
        self, monkeypatch, recording_log,
    ):
        instance = build()
        patch_repair_engine(monkeypatch, instance, poison_project_ids={2})
        #
        repaired, failed = instance.usage_reconcile_repair(
            [drift_row(1), drift_row(2), drift_row(3)],
        )
        #
        assert repaired == 2
        assert failed == [{"project_id": 2, "user_id": 7}]
        assert any("FAILED" in message for message in recording_log.messages("error"))

    def test_a_failed_row_is_reported_once_per_row_not_deduped(self, monkeypatch, recording_log):
        instance = build()
        patch_repair_engine(monkeypatch, instance, poison_project_ids={2, 3})
        #
        _repaired, failed = instance.usage_reconcile_repair(
            [drift_row(1), drift_row(2), drift_row(3)],
        )
        #
        assert failed == [{"project_id": 2, "user_id": 7}, {"project_id": 3, "user_id": 7}]
        assert len(recording_log.messages("error")) == 2

    def test_a_failed_row_never_reaches_commit(self, monkeypatch):
        instance = build()
        connections = patch_repair_engine(monkeypatch, instance, poison_project_ids={2})
        #
        repaired, failed = instance.usage_reconcile_repair([drift_row(2)])
        #
        assert repaired == 0
        assert failed == [{"project_id": 2, "user_id": 7}]
        assert all(connection.commits == 0 for connection in connections)


class TestReconcileRecordRunAndHistory:
    """Acceptance criterion 4: drift stays visible to a human even once it is auto-repaired."""

    def test_a_recorded_run_is_readable_back(self):
        instance = build()
        client = RecordingRedis()
        instance.usage_redis_client = lambda: client
        #
        summary = {"period": "202609", "drift": 2, "repaired": 2, "failed": 0}
        instance.usage_reconcile_record_run(summary)
        #
        assert instance.usage_reconcile_history() == [summary]

    def test_history_is_capped_to_the_configured_maximum(self):
        instance = build()
        client = RecordingRedis()
        instance.usage_redis_client = lambda: client
        #
        for i in range(reconcile.RECONCILE_HISTORY_MAX + 5):
            instance.usage_reconcile_record_run({"period": str(i)})
        #
        history = instance.usage_reconcile_history()
        assert len(history) == reconcile.RECONCILE_HISTORY_MAX
        assert history[-1]["period"] == str(reconcile.RECONCILE_HISTORY_MAX + 4)

    def test_recording_on_an_unreachable_redis_is_logged_not_raised(self, recording_log):
        instance = build()
        instance.usage_redis_client = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        #
        instance.usage_reconcile_record_run({"period": "202609"})  # must not raise
        #
        assert recording_log.records

    def test_reading_history_from_an_unreachable_redis_is_empty_not_raised(self):
        instance = build()
        instance.usage_redis_client = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        #
        assert instance.usage_reconcile_history() == []

    def test_an_unreadable_history_entry_is_skipped_not_fatal(self):
        instance = build()
        client = RecordingRedis()
        instance.usage_redis_client = lambda: client
        client.rpush(reconcile.RECONCILE_HISTORY_KEY, "{not json")
        instance.usage_reconcile_record_run({"period": "202609"})
        #
        assert instance.usage_reconcile_history() == [{"period": "202609"}]

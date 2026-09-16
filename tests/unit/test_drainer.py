"""Write-behind drainer — the reason a flushed-twice batch cannot double-count."""
import datetime
import json
import types

import pytest

from fixtures.fake_redis import RecordingRedis
from fixtures.helpers import bind, fake_module
from usage.methods import drainer
from usage.methods._counters import member_key, project_key

TS = datetime.datetime(2026, 9, 11, 12, 0, tzinfo=datetime.timezone.utc)


def event(key, project_id=42, user_id=7, cost=1000, event_type="llm"):
    """One usage_event row as the queue carries it."""
    row = {
        "idempotency_key": key, "ts": TS, "project_id": project_id, "user_id": user_id,
        "input_tokens": 10, "output_tokens": 20, "cost_micro_usd": cost,
        "event_type": event_type,
    }
    #
    return row


class Landing:
    """A connection that enforces the (idempotency_key, ts) unique index in memory.

    The whole double-count guarantee is "count the RETURNING rows, not the input batch", so the
    fake has to model exactly the thing the real index does: a repeat returns nothing.
    """

    def __init__(self, existing=()):
        self.landed = set(existing)
        self.upserts = []
        self.commits = 0

    def execute(self, statement, params=None):  # pylint: disable=W0613
        return _Result(self, statement)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Result:
    def __init__(self, connection, statement):
        self.connection = connection
        self.statement = statement

    def mappings(self):
        rows = getattr(self.statement, "_usage_rows", None) or []
        fresh = []
        #
        for row in rows:
            fingerprint = (row["idempotency_key"], row["ts"])
            #
            if fingerprint in self.connection.landed:
                continue
            #
            self.connection.landed.add(fingerprint)
            fresh.append(row)
        #
        return iter(fresh)


def build(connection, batch=500):
    """A Module with the drainer bound, recording counter upserts instead of writing them."""
    instance = fake_module()
    bind(instance, drainer.Method)
    instance.usage_config = lambda: {"redis": {"queue_flush_batch_size": batch}}
    #
    # Recorded rather than executed: building the real upsert needs a live dialect
    instance.usage_apply_counter_deltas = \
        lambda conn, deltas: connection.upserts.extend(deltas)
    #
    return instance


def insert(instance, connection, rows):
    """usage_insert_events, with the batch tagged so the fake result can replay it."""
    statement = _Tagged(rows)
    instance_rows = instance.usage_insert_events
    #
    real_insert = drainer.insert
    drainer.insert = lambda *a, **k: statement
    #
    try:
        return instance_rows(connection, rows)
    finally:
        drainer.insert = real_insert


class _Tagged:
    """Stands in for the built INSERT ... RETURNING, carrying the batch it was built from."""

    def __init__(self, rows):
        self._usage_rows = rows

    def values(self, *_args, **_kwargs):
        return self

    def on_conflict_do_nothing(self, **_kwargs):
        return self

    def on_conflict_do_update(self, **_kwargs):
        return self

    def returning(self, *_columns):
        return self


class TestCounterDeltas:
    """Deltas are derived from what landed, so the arithmetic is where correctness lives."""

    def test_one_event_writes_a_project_row_and_a_member_row(self):
        deltas = drainer.counter_deltas([event("a")])
        #
        wanted = [project_key(42, TS), member_key(42, 7, TS)]
        assert [
            {column: delta[column] for column in wanted[0]} for delta in deltas
        ] == [dict(wanted[0]), {**wanted[0], **{
            column: value for column, value in wanted[1].items() if column in wanted[0]
        }}]
        assert all(delta["cost_micro_usd"] == 1000 for delta in deltas)

    def test_an_event_without_a_user_writes_the_project_row_only(self):
        deltas = drainer.counter_deltas([event("a", user_id=None)])
        #
        assert len(deltas) == 1

    def test_events_sharing_a_key_are_summed_into_one_upsert(self):
        deltas = drainer.counter_deltas([event("a"), event("b")])
        #
        assert len(deltas) == 2
        assert all(delta["call_count"] == 2 for delta in deltas)
        assert all(delta["cost_micro_usd"] == 2000 for delta in deltas)

    def test_an_empty_landing_produces_no_upserts(self):
        assert drainer.counter_deltas([]) == []


class TestInsertEvents:
    def test_a_repeated_batch_lands_nothing_and_counts_nothing(self):
        connection = Landing()
        instance = build(connection)
        rows = [event("a"), event("b")]
        #
        assert len(insert(instance, connection, rows)) == 2
        first_pass = len(connection.upserts)
        #
        assert insert(instance, connection, rows) == []
        assert len(connection.upserts) == first_pass

    def test_a_partly_duplicate_batch_counts_only_the_new_rows(self):
        connection = Landing(existing=[("a", TS)])
        instance = build(connection)
        #
        landed = insert(instance, connection, [event("a"), event("b", cost=5000)])
        #
        assert [row["idempotency_key"] for row in landed] == ["b"]
        assert {delta["cost_micro_usd"] for delta in connection.upserts} == {5000}

    def test_a_tool_row_lands_as_a_fact_but_never_touches_the_counters(self):
        # It carries no cost, so only call_count drifted -- against facts reconcile filters out,
        # which left the drift report permanently non-empty
        connection = Landing()
        instance = build(connection)
        #
        landed = insert(instance, connection, [event("a", event_type="tool")])
        #
        assert [row["idempotency_key"] for row in landed] == ["a"]
        assert connection.upserts == []

    def test_a_mixed_batch_counts_only_its_llm_rows(self):
        connection = Landing()
        instance = build(connection)
        #
        insert(instance, connection, [event("a"), event("b", event_type="tool")])
        #
        assert {delta["call_count"] for delta in connection.upserts} == {1}


class TestQueue:
    def test_enqueue_reports_success(self):
        client = RecordingRedis()
        instance = build(Landing())
        instance.usage_redis_client = lambda: client
        #
        assert instance.usage_enqueue_event(event("a")) is True
        assert len(client.lists[drainer.QUEUE_KEY]) == 1

    def test_enqueue_reports_failure_so_the_caller_falls_back(self):
        instance = build(Landing())
        instance.usage_redis_client = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        #
        assert instance.usage_enqueue_event(event("a")) is False

    def test_dequeue_drops_an_unreadable_payload_rather_than_dying(self):
        client = RecordingRedis()
        instance = build(Landing())
        instance.usage_redis_client = lambda: client
        instance.usage_enqueue_event(event("a"))
        client.rpush(drainer.QUEUE_KEY, "{not json")
        #
        rows = instance.usage_dequeue_events()
        #
        assert [row["idempotency_key"] for row in rows] == ["a"]

    def test_dequeue_honours_the_configured_batch_size(self):
        client = RecordingRedis()
        instance = build(Landing(), batch=2)
        instance.usage_redis_client = lambda: client
        #
        for key in "abc":
            instance.usage_enqueue_event(event(key))
        #
        assert len(instance.usage_dequeue_events()) == 2

    def test_an_unreachable_queue_yields_an_empty_batch(self):
        instance = build(Landing())
        instance.usage_redis_client = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        #
        assert instance.usage_dequeue_events() == []


class TestRequeueOnFailure:
    """A dequeued batch is already out of Redis, so a failed tick must put it back."""

    def _instance(self, client, fail=True, monkeypatch=None):
        instance = build(Landing())
        instance.usage_redis_client = lambda: client
        #
        if fail:
            instance.usage_insert_events = \
                lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("postgres is down"))
        else:
            instance.usage_insert_events = lambda _conn, rows: rows
        #
        monkeypatch.setattr(
            drainer.db, "engine", types.SimpleNamespace(connect=Landing), raising=False,
        )
        #
        return instance

    def test_a_failed_tick_puts_the_batch_back(self, monkeypatch):
        client = RecordingRedis()
        instance = self._instance(client, monkeypatch=monkeypatch)
        instance.usage_enqueue_event(event("a"))
        instance.usage_enqueue_event(event("b"))
        #
        assert instance.usage_drain_batch() == 0
        #
        queued = [json.loads(item)["idempotency_key"] for item in client.lists[drainer.QUEUE_KEY]]
        assert queued == ["a", "b"]

    def test_the_requeued_batch_drains_on_the_next_tick(self, monkeypatch):
        client = RecordingRedis()
        instance = self._instance(client, monkeypatch=monkeypatch)
        instance.usage_enqueue_event(event("a"))
        instance.usage_drain_batch()
        #
        recovered = self._instance(client, fail=False, monkeypatch=monkeypatch)
        #
        assert recovered.usage_drain_batch() == 1

    def test_an_empty_batch_needs_no_requeue(self, monkeypatch):
        client = RecordingRedis()
        #
        assert self._instance(client, monkeypatch=monkeypatch).usage_drain_batch() == 0
        assert client.lists.get(drainer.QUEUE_KEY, []) == []

    def test_a_redis_that_cannot_take_the_batch_back_is_logged_not_raised(self, monkeypatch):
        class Broken(RecordingRedis):
            def lpush(self, key, value):
                raise RuntimeError("down")
        #
        client = Broken()
        instance = self._instance(client, monkeypatch=monkeypatch)
        instance.usage_enqueue_event(event("a"))
        #
        assert instance.usage_drain_batch() == 0

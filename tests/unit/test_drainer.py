"""Write-behind drainer — the reason a flushed-twice batch cannot double-count."""
import datetime
import json
import types

import pytest
from sqlalchemy.exc import CompileError

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


class TestEventValues:
    """A queued row is projected onto the model's own columns, as a copy.

    Both halves matter: the projection is what survives a column the model no longer has, and
    the copy is what keeps a failed tick from writing the drainer's own defaults back into Redis
    -- which is how a dropped column's key became permanently unusable queue content.
    """

    def test_a_key_with_no_column_is_dropped_not_raised(self):
        values = drainer.event_values([{**event("a"), "period": "202609"}])
        #
        assert "period" not in values[0]
        assert values[0]["idempotency_key"] == "a"

    def test_dropping_a_key_is_reported(self, recording_log):
        drainer.event_values([{**event("a"), "period": "202609"}])
        #
        assert any("period" in str(record) for record in recording_log.records)

    def test_a_clean_row_reports_nothing(self, recording_log):
        drainer.event_values([event("a")])
        #
        assert not recording_log.messages("warning")

    def test_the_caller_s_row_is_left_alone(self):
        row = event("a")
        #
        drainer.event_values([row])
        #
        assert set(row) == set(event("a"))

    def test_the_cost_split_is_defaulted_on_the_copy(self):
        values = drainer.event_values([event("a")])
        #
        assert all(values[0][column] == 0 for column in drainer.COST_SPLIT_COLUMNS)

    def test_a_stored_split_is_kept(self):
        values = drainer.event_values([{**event("a"), "input_cost_micro_usd": 900}])
        #
        assert values[0]["input_cost_micro_usd"] == 900


class TestPoisonRow:
    """A row that cannot be inserted must not starve the rows queued behind it."""

    def _instance(self, client, monkeypatch, broken=CompileError):
        instance = build(Landing())
        instance.usage_redis_client = lambda: client
        #
        def insert_events(_connection, rows):
            if any("period" in row for row in rows):
                raise broken("unconsumed column names: period")
            #
            return rows
        #
        instance.usage_insert_events = insert_events
        monkeypatch.setattr(
            drainer.db, "engine", types.SimpleNamespace(connect=Landing), raising=False,
        )
        #
        return instance

    def test_the_clean_rows_behind_it_still_land(self, monkeypatch):
        client = RecordingRedis()
        instance = self._instance(client, monkeypatch)
        instance.usage_enqueue_event({**event("bad"), "period": "202609"})
        instance.usage_enqueue_event(event("good"))
        #
        assert instance.usage_drain_batch() == 1

    def test_the_offender_leaves_the_queue_for_the_dead_letter_list(self, monkeypatch):
        client = RecordingRedis()
        instance = self._instance(client, monkeypatch)
        instance.usage_enqueue_event({**event("bad"), "period": "202609"})
        instance.usage_enqueue_event(event("good"))
        #
        instance.usage_drain_batch()
        #
        assert client.lists.get(drainer.QUEUE_KEY, []) == []
        retired = [json.loads(item) for item in client.lists[drainer.DEAD_QUEUE_KEY]]
        assert [row["idempotency_key"] for row in retired] == ["bad"]

    def test_a_second_tick_has_nothing_left_to_fail_on(self, monkeypatch):
        client = RecordingRedis()
        instance = self._instance(client, monkeypatch)
        instance.usage_enqueue_event({**event("bad"), "period": "202609"})
        instance.usage_drain_batch()
        #
        assert instance.usage_drain_batch() == 0
        assert client.lists.get(drainer.QUEUE_KEY, []) == []

    def test_a_failure_that_is_not_the_row_keeps_the_whole_batch_queued(self, monkeypatch):
        # An outage says nothing about the rows, and they are the only copy of the facts, so
        # anything but a row-shape failure goes back on the queue
        client = RecordingRedis()
        instance = self._instance(client, monkeypatch, broken=RuntimeError)
        instance.usage_enqueue_event({**event("bad"), "period": "202609"})
        instance.usage_enqueue_event(event("good"))
        #
        assert instance.usage_drain_batch() == 0
        assert len(client.lists[drainer.QUEUE_KEY]) == 2
        assert drainer.DEAD_QUEUE_KEY not in client.lists

    def test_a_requeued_row_is_byte_identical_to_what_was_queued(self, monkeypatch):
        # The requeue writes these rows back, so anything the insert path added to them would
        # become permanent queue content
        client = RecordingRedis()
        instance = self._instance(client, monkeypatch, broken=RuntimeError)
        original = {**event("a"), "period": "202609"}
        instance.usage_enqueue_event(original)
        queued = client.lists[drainer.QUEUE_KEY][0]
        #
        instance.usage_drain_batch()
        #
        assert client.lists[drainer.QUEUE_KEY] == [queued]


def drain_instance(client, monkeypatch, batch=2, batches=4):
    """A drainer whose tick allowance and batch size are both small enough to reason about.

    Shared by the multi-batch and backlog-reporting cases: they exercise the same loop from
    opposite ends (does it keep going / does it say so when it cannot finish).
    """
    instance = build(Landing())
    instance.usage_config = lambda: {"redis": {
        "queue_flush_batch_size": batch, "queue_flush_max_batches_per_tick": batches,
    }}
    instance.usage_redis_client = lambda: client
    instance.usage_insert_events = lambda _conn, rows: rows
    monkeypatch.setattr(
        drainer.db, "engine", types.SimpleNamespace(connect=Landing), raising=False,
    )
    #
    return instance


class TestMultiBatchTick:
    """One batch per tick capped the drainer at batch_size/interval rows per second; a burst
    above that grew the queue forever. A tick now drains several batches, stopping early."""

    def test_a_backlog_drains_several_batches_in_one_tick(self, monkeypatch):
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch)
        #
        for key in range(8):
            instance.usage_enqueue_event(event(str(key)))
        #
        assert instance.usage_drain_batch() == 8
        assert client.lists[drainer.QUEUE_KEY] == []

    def test_it_stops_at_the_configured_number_of_batches(self, monkeypatch):
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batches=2)
        #
        for key in range(8):
            instance.usage_enqueue_event(event(str(key)))
        #
        assert instance.usage_drain_batch() == 4
        assert len(client.lists[drainer.QUEUE_KEY]) == 4

    def test_the_ceiling_holds_even_when_configured_higher(self, monkeypatch):
        # The bound is the drain lease, so a config value above it must not be honoured
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batch=1, batches=999)
        #
        for key in range(20):
            instance.usage_enqueue_event(event(str(key)))
        #
        assert instance.usage_drain_batch() == drainer.MAX_BATCHES_PER_TICK_CEILING

    def test_an_empty_queue_costs_one_batch_read(self, monkeypatch):
        client = RecordingRedis()
        #
        assert drain_instance(client, monkeypatch).usage_drain_batch() == 0

    def test_a_failing_batch_stops_the_tick_rather_than_hammering(self, monkeypatch):
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch)
        instance.usage_insert_events = \
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("postgres is down"))
        #
        for key in range(8):
            instance.usage_enqueue_event(event(str(key)))
        #
        assert instance.usage_drain_batch() == 0
        assert len(client.lists[drainer.QUEUE_KEY]) == 8


class TestQueueDepth:
    def test_depth_is_the_queue_length(self):
        client = RecordingRedis()
        instance = build(Landing())
        instance.usage_redis_client = lambda: client
        instance.usage_enqueue_event(event("a"))
        #
        assert instance.usage_queue_depth() == 1

    def test_an_unreadable_queue_reports_no_depth(self):
        instance = build(Landing())
        instance.usage_redis_client = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        #
        assert instance.usage_queue_depth() is None


class TestBacklogReporting:
    """There is no configurable threshold: a tick that uses its full batch allowance and still
    finds the queue non-empty logs that fact on its own -- nothing for an operator to size."""

    def test_exhausting_the_allowance_with_rows_left_logs_loudly(self, monkeypatch, recording_log):
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batches=2)
        #
        for key in range(6):
            instance.usage_enqueue_event(event(str(key)))
        #
        instance.usage_drain_batch()
        #
        errors = recording_log.messages("error")
        assert len(errors) == 1
        assert "fell behind" in errors[0]

    def test_emptying_the_queue_within_the_allowance_says_nothing(self, monkeypatch, recording_log):
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batches=2)
        #
        for key in range(3):
            instance.usage_enqueue_event(event(str(key)))
        #
        instance.usage_drain_batch()
        #
        assert not recording_log.messages("error")

    def test_emptying_the_queue_exactly_on_the_last_batch_says_nothing(self, monkeypatch, recording_log):
        # The loop's else-branch fires (every batch came back full), but depth is checked
        # freshly rather than trusting that -- so an exact fit stays silent
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batches=2)
        #
        for key in range(4):
            instance.usage_enqueue_event(event(str(key)))
        #
        instance.usage_drain_batch()
        #
        assert not recording_log.messages("error")

    def test_repeated_backlog_logs_every_tick_not_deduped(self, monkeypatch, recording_log):
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batches=2)
        #
        for key in range(10):
            instance.usage_enqueue_event(event(str(key)))
        #
        instance.usage_drain_batch()
        instance.usage_drain_batch()
        #
        assert len(recording_log.messages("error")) == 2

    def test_no_reporting_path_shortens_the_queue(self, monkeypatch):
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batches=2)
        #
        for key in range(6):
            instance.usage_enqueue_event(event(str(key)))
        #
        instance.usage_drain_batch()
        #
        assert len(client.lists[drainer.QUEUE_KEY]) == 2

    def test_a_write_failure_reports_the_depth_it_leaves_behind(self, monkeypatch, recording_log):
        # An outage lands zero rows, exactly like an empty queue does. Reported as a plain
        # traceback it looked like a hiccup; what an operator needs is how much is piling up
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batches=4)
        instance.usage_insert_events = \
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("postgres is down"))
        #
        for key in range(6):
            instance.usage_enqueue_event(event(str(key)))
        #
        assert instance.usage_drain_batch() == 0
        #
        errors = [record for record in recording_log.records if record[0] == "error"]
        assert len(errors) == 1
        assert "fell behind" in errors[0][1]
        # The whole point of routing this path through the reporter: the depth is in the line
        assert errors[0][2][0] == 6
        assert len(client.lists[drainer.QUEUE_KEY]) == 6

    def test_a_write_failure_does_not_spend_the_rest_of_the_allowance(self, monkeypatch):
        # Re-dequeueing the same rows against a database that just refused them is pure load
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batches=4)
        attempts = []
        #
        def insert_events(_connection, rows):
            attempts.append(len(rows))
            raise RuntimeError("postgres is down")
        #
        instance.usage_insert_events = insert_events
        #
        for key in range(8):
            instance.usage_enqueue_event(event(str(key)))
        #
        instance.usage_drain_batch()
        #
        assert attempts == [2]

    def test_a_partly_failed_isolated_pass_stops_the_tick(self, monkeypatch, recording_log):
        # The isolated pass costs a connection per row, and it can land some rows while
        # requeueing others -- a nonzero count there must not read as "healthy, keep going"
        client = RecordingRedis()
        instance = drain_instance(client, monkeypatch, batch=3, batches=4)
        attempts = []
        #
        def insert_events(_connection, rows):
            attempts.append([row["idempotency_key"] for row in rows])
            #
            if any("period" in row for row in rows):
                raise CompileError("unconsumed column names: period")
            #
            if any(row["idempotency_key"] == "flaky" for row in rows):
                raise RuntimeError("deadlock detected")
            #
            return rows
        #
        instance.usage_insert_events = insert_events
        instance.usage_enqueue_event({**event("bad"), "period": "202609"})
        instance.usage_enqueue_event(event("flaky"))
        instance.usage_enqueue_event(event("good"))
        #
        for key in range(3):
            instance.usage_enqueue_event(event(str(key)))
        #
        assert instance.usage_drain_batch() == 1
        # One batch build, then one attempt per row of it -- never a second batch
        assert len(attempts) == 4
        #
        queued = [json.loads(item)["idempotency_key"] for item in client.lists[drainer.QUEUE_KEY]]
        assert queued == ["flaky", "0", "1", "2"]
        assert any("fell behind" in message for message in recording_log.messages("error"))

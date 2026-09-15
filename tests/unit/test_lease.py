"""Worker lease — exactly one replica drains, and a dead replica hands over."""
import pytest

from fixtures.fake_redis import RecordingRedis
from fixtures.helpers import bind, fake_module
from usage.methods import gate, workers


def build(client, replica_id, config=None):
    """A Module with the worker helpers bound, pinned to one replica identity."""
    instance = fake_module()
    bind(instance, workers.Method)
    instance.usage_config = lambda: config or {}
    instance.usage_redis_client = lambda: client
    #
    return instance


@pytest.fixture
def replicas():
    """Two Modules sharing one Redis, each with its own replica id."""
    client = RecordingRedis()
    first, second = "replica-one", "replica-two"
    #
    return client, build(client, first), build(client, second), first, second


def pin(instance, replica_id, monkeypatch):
    monkeypatch.setattr(workers, "REPLICA_ID", replica_id)
    #
    return instance


class TestLease:
    def test_the_first_replica_wins(self, replicas, monkeypatch):
        client, first, _, first_id, _ = replicas
        monkeypatch.setattr(workers, "REPLICA_ID", first_id)
        #
        assert first.usage_hold_lease(gate.DRAIN_LEASE_KEY) is True
        assert client.get(gate.DRAIN_LEASE_KEY) == first_id

    def test_the_loser_idles_rather_than_draining_too(self, replicas, monkeypatch):
        client, first, second, first_id, second_id = replicas
        monkeypatch.setattr(workers, "REPLICA_ID", first_id)
        first.usage_hold_lease(gate.DRAIN_LEASE_KEY)
        #
        monkeypatch.setattr(workers, "REPLICA_ID", second_id)
        #
        assert second.usage_hold_lease(gate.DRAIN_LEASE_KEY) is False
        assert client.get(gate.DRAIN_LEASE_KEY) == first_id

    def test_the_holder_keeps_refreshing_its_own_lease(self, replicas, monkeypatch):
        client, first, _, first_id, _ = replicas
        monkeypatch.setattr(workers, "REPLICA_ID", first_id)
        #
        assert first.usage_hold_lease(gate.DRAIN_LEASE_KEY) is True
        assert first.usage_hold_lease(gate.DRAIN_LEASE_KEY) is True

    def test_expiry_hands_the_lease_to_the_survivor(self, replicas, monkeypatch):
        # A replica that dies holding the lease must not stop the others forever
        client, first, second, first_id, second_id = replicas
        monkeypatch.setattr(workers, "REPLICA_ID", first_id)
        first.usage_hold_lease(gate.DRAIN_LEASE_KEY)
        #
        client.force_lease_expiry(gate.DRAIN_LEASE_KEY)
        monkeypatch.setattr(workers, "REPLICA_ID", second_id)
        #
        assert second.usage_hold_lease(gate.DRAIN_LEASE_KEY) is True
        assert client.get(gate.DRAIN_LEASE_KEY) == second_id

    def test_the_drainer_and_the_reaper_hold_separate_leases(self, replicas, monkeypatch):
        """Otherwise one replica could take both and the other would idle uselessly."""
        _, first, second, first_id, second_id = replicas
        monkeypatch.setattr(workers, "REPLICA_ID", first_id)
        first.usage_hold_lease(gate.DRAIN_LEASE_KEY)
        #
        monkeypatch.setattr(workers, "REPLICA_ID", second_id)
        #
        assert second.usage_hold_lease(gate.REAP_LEASE_KEY) is True

    def test_an_unreachable_redis_never_grants_the_lease(self, replicas):
        _, first, _, _, _ = replicas
        first.usage_redis_client = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        #
        assert first.usage_hold_lease(gate.DRAIN_LEASE_KEY) is False

    def test_the_lease_carries_a_configured_expiry(self, replicas, monkeypatch):
        client, first, _, first_id, _ = replicas
        monkeypatch.setattr(workers, "REPLICA_ID", first_id)
        first.usage_config = lambda: {"redis": {"lease_seconds": 7}}
        #
        first.usage_hold_lease(gate.DRAIN_LEASE_KEY)
        #
        assert ("set", gate.DRAIN_LEASE_KEY, first_id, 7) in client.calls


class TestStartWorkers:
    def test_starting_twice_does_not_double_the_threads(self):
        started = []
        instance = fake_module()
        bind(instance, workers.Method)
        instance.usage_config = lambda: {}
        instance.usage_redis_client = lambda: RecordingRedis()
        instance.usage_drain_batch = lambda: None
        instance.usage_reap_expired = lambda: None
        instance.usage_worker_loop = lambda *a, **k: started.append(a[0])
        #
        instance.usage_start_workers()
        instance.usage_start_workers()
        #
        assert sorted(started) == ["usage-drainer", "usage-reaper"]

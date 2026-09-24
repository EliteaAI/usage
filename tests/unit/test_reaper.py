"""Reservation reaper — releasing expired holds and pruning the index it scans."""
import datetime

import pytest

from fixtures.fake_redis import RecordingRedis
from fixtures.helpers import bind, fake_module
from usage.methods import gate, reaper

MOMENT = datetime.datetime(2026, 9, 11, 12, 0, tzinfo=datetime.timezone.utc)

MILLION = 1_000_000


@pytest.fixture(autouse=True)
def clean_caches():
    """The gate's limits and priming caches are module state shared across tests."""
    gate._limits_cache = None  # pylint: disable=W0212
    gate._primed.clear()  # pylint: disable=W0212
    yield
    gate._limits_cache = None  # pylint: disable=W0212
    gate._primed.clear()  # pylint: disable=W0212


def build():
    """A Module with the gate and the reaper bound over one fake Redis."""
    client = RecordingRedis()
    limits = {
        "project_limit_nano": 100 * MILLION, "member_limit_nano": None,
        "enabled": True, "is_personal_project": False,
    }
    instance = fake_module(config={"usage": {}})
    bind(instance, gate.Method, reaper.Method)
    instance.usage_config = lambda: {}
    instance.usage_redis_client = lambda: client
    instance.usage_counter_of = lambda key: 0
    instance.usage_gate_limits = lambda project_id, user_id=None: limits
    #
    return instance, client


def index_member():
    return gate.resv_index_member(42, MOMENT)


class TestReaping:
    def test_an_expired_reservation_is_released(self):
        instance, client = build()
        instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        # Age every hold past its deadline without waiting out the TTL
        resv = gate.resv_key(42, MOMENT)
        client.zsets[resv] = {member: 0 for member in client.zsets[resv]}
        #
        assert instance.usage_reap_expired() == 1
        assert client.reserved(gate.project_hash_key(42, MOMENT)) == 0

    def test_a_live_reservation_is_left_alone(self):
        instance, client = build()
        instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        #
        assert instance.usage_reap_expired() == 0
        assert client.reserved(gate.project_hash_key(42, MOMENT)) == MILLION


class TestIndexPruning:
    def test_a_drained_bucket_leaves_the_index(self):
        # Or the reaper scans every month ever gated, forever
        instance, client = build()
        reservation = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["reservation"]
        instance.usage_gate_settle(reservation, MILLION)
        #
        instance.usage_reap_bucket(index_member())
        #
        assert index_member() not in client.smembers(gate.RESV_INDEX_KEY)

    def test_a_bucket_with_a_live_reservation_stays_indexed(self):
        instance, client = build()
        instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        #
        instance.usage_reap_bucket(index_member())
        #
        assert index_member() in client.smembers(gate.RESV_INDEX_KEY)

    def test_the_check_and_the_removal_are_one_round_trip(self):
        # A separate ZCARD-then-SREM lets a concurrent acquire land in between, dropping a
        # bucket that now holds a live reservation the reaper can no longer see
        instance, client = build()
        reservation = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["reservation"]
        instance.usage_gate_settle(reservation, MILLION)
        before = len(client.calls)
        #
        instance.usage_reap_bucket(index_member())
        #
        prunes = [call for call in client.calls[before:] if call[0] == "eval"]
        assert prunes == [("eval", [gate.resv_key(42, MOMENT), gate.RESV_INDEX_KEY],
                          [index_member()])]

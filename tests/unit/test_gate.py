"""Admission gate — every branch that decides whether money may be spent."""
import datetime
import json

import pytest

from fixtures.fake_redis import RecordingRedis
from fixtures.helpers import bind, fake_module
from usage.methods import gate

MOMENT = datetime.datetime(2026, 9, 11, 12, 0, tzinfo=datetime.timezone.utc)

MILLION = 1_000_000


@pytest.fixture(autouse=True)
def clean_caches():
    """The limits cache and the priming cache are module state shared across tests."""
    gate._limits_cache = None  # pylint: disable=W0212
    gate._primed.clear()  # pylint: disable=W0212
    yield
    gate._limits_cache = None  # pylint: disable=W0212
    gate._primed.clear()  # pylint: disable=W0212


def build(limits, redis=None, persisted=0):
    """A Module with the gate bound, a fake Redis and a fixed answer from the limit ladder."""
    client = redis if redis is not None else RecordingRedis()
    instance = fake_module(
        config={"usage": {}},
        usage_config=lambda: {},
        usage_redis_client=lambda: client,
        usage_counter_of=lambda key: persisted,
        usage_gate_limits=lambda project_id, user_id=None: limits,
    )
    bind(instance, gate.Method)
    # bind() would otherwise re-install the real implementations over the stubs above
    instance.usage_config = lambda: {}
    instance.usage_redis_client = lambda: client
    instance.usage_counter_of = lambda key: persisted
    instance.usage_gate_limits = lambda project_id, user_id=None: limits
    #
    return instance, client


def enabled(project=None, member=None):
    return {
        "project_limit_micro": project, "member_limit_micro": member,
        "enabled": True, "is_personal_project": False,
    }


class TestAcquire:
    """available = limit - (counter + reserved)."""

    def test_grants_and_reserves_the_estimate(self):
        instance, client = build(enabled(project=10 * MILLION))
        #
        verdict = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        #
        assert verdict["allowed"] is True
        assert verdict["healthy"] is True
        assert client.reserved(gate.project_hash_key(42, MOMENT)) == MILLION
        assert client.reserved(gate.member_hash_key(42, 7, MOMENT)) == MILLION

    def test_denies_on_the_project_limit_and_names_the_scope(self):
        instance, client = build(enabled(project=MILLION), persisted=MILLION)
        #
        verdict = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        #
        assert verdict == {
            "allowed": False, "scope": "project", "reservation": None, "healthy": True,
        }
        assert client.reserved(gate.project_hash_key(42, MOMENT)) == 0

    def test_denies_on_the_member_limit_even_with_project_headroom(self):
        instance, _ = build(enabled(project=100 * MILLION, member=MILLION))
        #
        assert instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["allowed"] is True
        #
        verdict = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        #
        assert verdict["scope"] == "member"

    def test_an_unlimited_limit_never_denies(self):
        instance, _ = build(enabled(project=None, member=None))
        #
        verdict = instance.usage_gate_acquire(42, 7, 10 ** 12, MOMENT)
        #
        assert verdict["allowed"] is True

    def test_a_zero_estimate_still_passes_an_exhausted_budget(self):
        # An unpriced model estimates 0; it must be metered, not refused
        instance, _ = build(enabled(project=MILLION), persisted=MILLION)
        #
        assert instance.usage_gate_acquire(42, 7, 0, MOMENT)["allowed"] is True

    def test_disabled_budgets_short_circuit_before_redis(self):
        instance, client = build({"enabled": False})
        #
        verdict = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        #
        assert verdict == {
            "allowed": True, "scope": None, "reservation": None, "healthy": True,
        }
        assert client.calls == []

    def test_a_missing_user_gates_on_the_project_alone(self):
        instance, client = build(enabled(project=10 * MILLION, member=MILLION))
        #
        assert instance.usage_gate_acquire(42, None, 5 * MILLION, MOMENT)["allowed"] is True
        assert client.hashes.get(gate.member_hash_key(42, 0, MOMENT)) is None

    def test_an_unreachable_redis_reports_unhealthy_rather_than_denying(self):
        instance, _ = build(enabled(project=10 * MILLION))
        instance.usage_redis_client = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        #
        verdict = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        #
        assert verdict["healthy"] is False
        assert verdict["allowed"] is False

    def test_an_unresolvable_limit_reports_unhealthy(self):
        """Unlimited and unknown must never look the same to the caller."""
        instance, _ = build(enabled())
        instance.usage_gate_limits = lambda project_id, user_id=None: 1 / 0
        #
        assert instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["healthy"] is False


class TestSettle:
    """Release is once-only; accrual is not."""

    def test_releases_the_reservation_and_accrues_the_actual_cost(self):
        instance, client = build(enabled(project=10 * MILLION))
        reservation = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["reservation"]
        #
        assert instance.usage_gate_settle(reservation, 3 * MILLION) is True
        #
        project_hash = gate.project_hash_key(42, MOMENT)
        assert client.reserved(project_hash) == 0
        assert client.counter(project_hash) == 3 * MILLION

    def test_settling_twice_decrements_the_reservation_once(self):
        instance, client = build(enabled(project=100 * MILLION))
        reservation = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["reservation"]
        #
        instance.usage_gate_settle(reservation, 0)
        #
        assert instance.usage_gate_settle(reservation, 0) is False
        assert client.reserved(gate.project_hash_key(42, MOMENT)) == 0

    def test_a_reaped_reservation_still_bills_what_the_call_cost(self):
        # A stream longer than the TTL is released by the reaper, then settles for real
        instance, client = build(enabled(project=100 * MILLION))
        reservation = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["reservation"]
        instance.usage_gate_release(reservation)
        #
        instance.usage_gate_settle(reservation, 5 * MILLION)
        #
        project_hash = gate.project_hash_key(42, MOMENT)
        assert client.counter(project_hash) == 5 * MILLION
        assert client.reserved(project_hash) == 0

    def test_release_bills_nothing(self):
        instance, client = build(enabled(project=100 * MILLION))
        reservation = instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["reservation"]
        #
        assert instance.usage_gate_release(reservation) is True
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 0

    def test_a_malformed_reservation_is_refused_not_raised(self):
        instance, _ = build(enabled())
        #
        assert instance.usage_gate_settle("not json", 1) is False

    def test_the_reservation_carries_the_keys_settle_needs(self):
        """settle must not have to recompute the period: a month can roll mid-call."""
        instance, _ = build(enabled(project=10 * MILLION))
        #
        parsed = json.loads(instance.usage_gate_acquire(42, 7, MILLION, MOMENT)["reservation"])
        #
        assert parsed["pk"] == gate.project_hash_key(42, MOMENT)
        assert parsed["mk"] == gate.member_hash_key(42, 7, MOMENT)
        assert parsed["rk"] == gate.resv_key(42, MOMENT)
        assert parsed["est"] == MILLION


class TestPriming:
    """A cold or evicted hash must be seeded from Postgres or spend runs away unbounded."""

    def test_primes_the_persisted_counter_exactly_once(self):
        instance, client = build(enabled(project=100 * MILLION), persisted=7 * MILLION)
        #
        instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        instance.usage_gate_acquire(42, 7, MILLION, MOMENT)
        #
        primes = [call for call in client.calls if call[0] == "eval" and len(call[1]) == 1]
        assert len(primes) == 2  # once for the project hash, once for the member hash
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 7 * MILLION

    def test_priming_never_lowers_a_counter_that_is_ahead(self):
        instance, client = build(enabled(project=100 * MILLION), persisted=7 * MILLION)
        client.hset(gate.project_hash_key(42, MOMENT), "counter", 9 * MILLION)
        #
        instance.usage_gate_acquire(42, None, MILLION, MOMENT)
        #
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 9 * MILLION

    def test_priming_raises_a_key_left_stale_while_budgets_were_disabled(self):
        # A hash primed at 0 before limits were removed keeps existing, so the spend that
        # accrued meanwhile lives only in Postgres — enforcement must resume from that figure
        instance, client = build(enabled(project=100 * MILLION), persisted=7 * MILLION)
        client.hset(gate.project_hash_key(42, MOMENT), "counter", 0)
        #
        instance.usage_gate_acquire(42, None, MILLION, MOMENT)
        #
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 7 * MILLION

    def test_a_failed_prime_is_retried_on_the_next_call(self):
        instance, client = build(enabled(project=100 * MILLION))
        broken = {"first": True}
        real_eval = client.eval
        #
        def failing_eval(script, numkeys, *args):
            if script == gate.PRIME_LUA and broken.pop("first", None):
                raise RuntimeError("down")
            #
            return real_eval(script, numkeys, *args)
        #
        client.eval = failing_eval
        #
        assert instance.usage_gate_acquire(42, None, MILLION, MOMENT)["healthy"] is False
        assert instance.usage_gate_acquire(42, None, MILLION, MOMENT)["healthy"] is True


class TestKeys:
    """The key schema is a wire contract with the reaper and the reconcile."""

    def test_period_is_the_month_the_moment_falls_in(self):
        assert gate.period_of(MOMENT) == "202609"

    def test_the_index_member_round_trips_to_its_zset_key(self):
        member = gate.resv_index_member(42, MOMENT)
        #
        assert gate.resv_key_of_index_member(member) == gate.resv_key(42, MOMENT)

    def test_unlimited_is_minus_one_on_the_wire(self):
        assert gate.as_limit(None) == gate.UNLIMITED
        assert gate.as_limit(0) == 0
        assert gate.as_limit(-5) == 0


class TestDoorCheck:
    """The predict-door pre-check: read-only, so it must never reserve or deny wrongly."""

    def _spend(self, client, key, counter=0, reserved=0):
        client.hashes[key] = {"counter": counter, "reserved": reserved}

    def test_an_exhausted_project_closes_the_door(self):
        instance, client = build(enabled(project=10 * MILLION))
        self._spend(client, gate.project_hash_key(42, MOMENT), counter=10 * MILLION)
        #
        verdict = instance.usage_gate_check(42, 7, MOMENT)
        #
        assert verdict == {"closed": True, "scope": gate.SCOPE_PROJECT, "healthy": True}

    def test_headroom_keeps_the_door_open(self):
        instance, client = build(enabled(project=10 * MILLION))
        self._spend(client, gate.project_hash_key(42, MOMENT), counter=9 * MILLION)
        #
        assert instance.usage_gate_check(42, 7, MOMENT)["closed"] is False

    def test_outstanding_reservations_count_towards_the_limit(self):
        instance, client = build(enabled(project=10 * MILLION))
        self._spend(
            client, gate.project_hash_key(42, MOMENT),
            counter=6 * MILLION, reserved=4 * MILLION,
        )
        #
        assert instance.usage_gate_check(42, 7, MOMENT)["closed"] is True

    def test_a_full_member_slice_closes_on_member_scope(self):
        instance, client = build(enabled(project=100 * MILLION, member=MILLION))
        self._spend(client, gate.member_hash_key(42, 7, MOMENT), counter=MILLION)
        #
        verdict = instance.usage_gate_check(42, 7, MOMENT)
        #
        assert verdict == {"closed": True, "scope": gate.SCOPE_MEMBER, "healthy": True}

    def test_a_member_limit_is_ignored_without_a_user(self):
        instance, client = build(enabled(project=100 * MILLION, member=MILLION))
        self._spend(client, gate.member_hash_key(42, 7, MOMENT), counter=MILLION)
        #
        assert instance.usage_gate_check(42, None, MOMENT)["closed"] is False

    def test_unlimited_budgets_never_close(self):
        instance, client = build(enabled())
        self._spend(client, gate.project_hash_key(42, MOMENT), counter=999 * MILLION)
        #
        assert instance.usage_gate_check(42, 7, MOMENT)["closed"] is False

    def test_disabled_budgets_short_circuit_before_redis(self):
        instance, client = build({"enabled": False})
        #
        assert instance.usage_gate_check(42, 7, MOMENT) == {
            "closed": False, "scope": None, "healthy": True,
        }
        assert client.calls == []

    def test_checking_reserves_nothing(self):
        instance, client = build(enabled(project=10 * MILLION))
        #
        instance.usage_gate_check(42, 7, MOMENT)
        #
        assert client.reserved(gate.project_hash_key(42, MOMENT)) == 0

    def test_an_unreachable_redis_is_unknown_not_closed(self):
        class Broken:
            def hmget(self, *args):
                raise RuntimeError("redis is down")
        #
        instance, _ = build(enabled(project=MILLION), redis=Broken())
        #
        assert instance.usage_gate_check(42, 7, MOMENT) == {
            "closed": False, "scope": None, "healthy": False,
        }

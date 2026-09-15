"""Admission gate — every branch that decides whether money may be spent."""
import datetime

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


class TestCheck:
    """The whole admission decision: closed once the counter has reached the limit."""

    def _spend(self, client, key, counter=0):
        client.hashes[key] = {"counter": counter}

    def test_an_exhausted_project_closes_the_gate(self):
        instance, client = build(enabled(project=10 * MILLION))
        self._spend(client, gate.project_hash_key(42, MOMENT), counter=10 * MILLION)
        #
        verdict = instance.usage_gate_check(42, 7, MOMENT)
        #
        assert verdict == {"closed": True, "scope": gate.SCOPE_PROJECT, "healthy": True}

    def test_headroom_keeps_the_gate_open(self):
        instance, client = build(enabled(project=10 * MILLION))
        self._spend(client, gate.project_hash_key(42, MOMENT), counter=9 * MILLION)
        #
        assert instance.usage_gate_check(42, 7, MOMENT)["closed"] is False

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

    def test_checking_spends_nothing(self):
        instance, client = build(enabled(project=10 * MILLION))
        #
        instance.usage_gate_check(42, 7, MOMENT)
        #
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 0

    def test_an_unreachable_redis_is_unknown_not_closed(self):
        class Broken:
            def hget(self, *args):
                raise RuntimeError("redis is down")
        #
        instance, _ = build(enabled(project=MILLION), redis=Broken())
        #
        assert instance.usage_gate_check(42, 7, MOMENT) == {
            "closed": False, "scope": None, "healthy": False,
        }

    def test_an_unresolvable_limit_reports_unhealthy(self):
        """Unlimited and unknown must never look the same to the caller."""
        instance, _ = build(enabled())
        instance.usage_gate_limits = lambda project_id, user_id=None: 1 / 0
        #
        assert instance.usage_gate_check(42, 7, MOMENT)["healthy"] is False


class TestAccrue:
    """What a finished call cost has to land on the counters the gate reads."""

    def test_accrues_to_both_the_project_and_the_member(self):
        instance, client = build(enabled(project=100 * MILLION))
        #
        assert instance.usage_gate_accrue(42, 7, 3 * MILLION, MOMENT) is True
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 3 * MILLION
        assert client.counter(gate.member_hash_key(42, 7, MOMENT)) == 3 * MILLION

    def test_accruals_add_up_across_calls(self):
        instance, client = build(enabled(project=100 * MILLION))
        #
        instance.usage_gate_accrue(42, 7, MILLION, MOMENT)
        instance.usage_gate_accrue(42, 7, 2 * MILLION, MOMENT)
        #
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 3 * MILLION

    def test_a_free_call_touches_nothing(self):
        # An unpriced model records cost 0; writing it would only churn the hash
        instance, client = build(enabled(project=100 * MILLION))
        #
        assert instance.usage_gate_accrue(42, 7, 0, MOMENT) is False
        assert client.hashes == {}

    def test_a_missing_user_accrues_to_the_project_alone(self):
        instance, client = build(enabled(project=100 * MILLION))
        #
        instance.usage_gate_accrue(42, None, MILLION, MOMENT)
        #
        assert client.counter(gate.project_hash_key(42, MOMENT)) == MILLION
        assert gate.member_hash_key(42, 0, MOMENT) not in client.hashes

    def test_a_broken_redis_is_reported_not_raised(self):
        instance, _ = build(enabled(project=100 * MILLION))
        instance.usage_redis_client = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        #
        assert instance.usage_gate_accrue(42, 7, MILLION, MOMENT) is False


class TestPriming:
    """A cold or evicted hash must be seeded from Postgres or spend runs away unbounded."""

    def test_primes_each_hash_exactly_once(self):
        instance, client = build(
            enabled(project=100 * MILLION, member=50 * MILLION), persisted=7 * MILLION,
        )
        #
        instance.usage_gate_check(42, 7, MOMENT)
        instance.usage_gate_check(42, 7, MOMENT)
        #
        primes = [call for call in client.calls if call[0] == "eval"]
        assert len(primes) == 2  # once for the project hash, once for the member hash
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 7 * MILLION

    def test_priming_never_lowers_a_counter_that_is_ahead(self):
        instance, client = build(enabled(project=100 * MILLION), persisted=7 * MILLION)
        client.hset(gate.project_hash_key(42, MOMENT), "counter", 9 * MILLION)
        #
        instance.usage_gate_check(42, None, MOMENT)
        #
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 9 * MILLION

    def test_priming_raises_a_key_left_stale_while_budgets_were_disabled(self):
        # A hash primed at 0 before limits were removed keeps existing, so the spend that
        # accrued meanwhile lives only in Postgres — enforcement must resume from that figure
        instance, client = build(enabled(project=100 * MILLION), persisted=7 * MILLION)
        client.hset(gate.project_hash_key(42, MOMENT), "counter", 0)
        #
        instance.usage_gate_check(42, None, MOMENT)
        #
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 7 * MILLION

    def test_an_unlimited_scope_is_never_primed(self):
        instance, client = build(enabled(), persisted=7 * MILLION)
        #
        instance.usage_gate_check(42, 7, MOMENT)
        #
        assert client.calls == []

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
        assert instance.usage_gate_check(42, None, MOMENT)["healthy"] is False
        assert instance.usage_gate_check(42, None, MOMENT)["healthy"] is True


    def test_an_eviction_inside_the_primed_window_is_re_primed(self):
        # _primed remembers only the key name, so without noticing the hash is gone the gate
        # would enforce against zero for the rest of the hour while the spend lives in Postgres
        instance, client = build(enabled(project=100 * MILLION), persisted=7 * MILLION)
        instance.usage_gate_check(42, None, MOMENT)
        client.hashes.pop(gate.project_hash_key(42, MOMENT))
        #
        instance.usage_gate_check(42, None, MOMENT)
        #
        assert client.counter(gate.project_hash_key(42, MOMENT)) == 7 * MILLION

    def test_an_evicted_key_re_primed_above_its_limit_closes_the_gate(self):
        instance, client = build(enabled(project=5 * MILLION), persisted=7 * MILLION)
        instance.usage_gate_check(42, None, MOMENT)
        client.hashes.pop(gate.project_hash_key(42, MOMENT))
        #
        assert instance.usage_gate_check(42, None, MOMENT)["closed"] is True

    def test_a_zero_spend_project_is_not_re_primed_on_every_call(self):
        # Priming a persisted 0 leaves the field absent, which must not be read as an eviction
        instance, client = build(enabled(project=100 * MILLION), persisted=0)
        #
        for _ in range(3):
            instance.usage_gate_check(42, None, MOMENT)
        #
        assert len([call for call in client.calls if call[0] == "eval"]) == 1


class TestKeys:
    """The key schema is a wire contract with the drainer and the reconcile."""

    def test_period_is_the_month_the_moment_falls_in(self):
        assert gate.period_of(MOMENT) == "202609"

    def test_the_project_and_member_hashes_are_distinct(self):
        assert gate.project_hash_key(42, MOMENT) == "usage:ctr:p:42:202609"
        assert gate.member_hash_key(42, 7, MOMENT) == "usage:ctr:u:42:7:202609"

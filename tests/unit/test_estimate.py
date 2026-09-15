"""What a call is reserved for before anyone knows what it costs."""
import pytest

from fixtures.helpers import bind, fake_module
from usage.methods import estimate


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    """estimate.py prices through the module-level rpc context, not self.context."""
    monkeypatch.setattr(estimate, "context", _Context())


def build(cost=None, reservation=None, raises=False):
    """A Module with the estimator bound and a stubbed price catalog."""
    calls = []
    #
    def compute(**kwargs):
        calls.append(kwargs)
        #
        if raises:
            raise RuntimeError("catalog unreachable")
        #
        return None if cost is None else {"cost": cost}
    #
    instance = fake_module()
    bind(instance, estimate.Method)
    instance.usage_config = lambda: {"reservation": reservation or {}}
    estimate.context.rpc_manager = _Manager(compute)
    #
    return instance, calls


class _Context:
    rpc_manager = None


class _Manager:
    def __init__(self, compute):
        self._compute = compute

    def timeout(self, _seconds):
        return self

    def costs_compute_llm_cost(self, **kwargs):
        return self._compute(**kwargs)


class TestOutputTokens:
    """The reservation follows what the caller actually asked the model for."""

    @pytest.mark.parametrize("field", estimate.OUTPUT_LIMIT_FIELDS)
    def test_any_of_the_output_limit_fields_is_honoured(self, field):
        assert estimate.output_tokens_of({field: 250}) == 250

    def test_falls_back_to_the_default_when_none_is_named(self):
        assert estimate.output_tokens_of({"model": "x"}) == estimate.DEFAULT_OUTPUT_TOKENS

    def test_the_default_is_configurable(self):
        assert estimate.output_tokens_of({}, default_output_tokens=99) == 99

    @pytest.mark.parametrize("value", [0, -5, None, "many", {}])
    def test_a_useless_value_falls_back_rather_than_raising(self, value):
        assert estimate.output_tokens_of({"max_tokens": value}) == estimate.DEFAULT_OUTPUT_TOKENS

    def test_a_body_that_is_not_a_mapping_is_tolerated(self):
        assert estimate.output_tokens_of(None) == estimate.DEFAULT_OUTPUT_TOKENS


class TestInputTokens:
    """Content-Length only: re-serializing a body to measure it is a known outage class."""

    def test_bytes_become_tokens(self):
        assert estimate.input_tokens_of(400) == 100

    @pytest.mark.parametrize("value", [None, "", -10, "junk"])
    def test_a_missing_or_bad_length_is_zero(self, value):
        assert estimate.input_tokens_of(value) == 0


class TestEstimateMicro:
    def test_prices_the_call_in_micro_usd(self):
        instance, calls = build(cost=0.25)
        #
        assert instance.usage_estimate_micro("gpt-5", 1000, 400) == 250_000
        assert calls == [{
            "model_name": "gpt-5", "input_tokens": 100, "output_tokens": 1000,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }]

    def test_clamps_to_the_configured_max_call_cost(self):
        # The published overspend bound is concurrency x max single-call cost; without the
        # clamp one absurd max_tokens would make the bound meaningless
        instance, _ = build(cost=50.0, reservation={"max_call_cost_usd": 2.0})
        #
        assert instance.usage_estimate_micro("gpt-5", 10 ** 7, 0) == 2_000_000

    def test_an_unpriced_model_reserves_nothing(self):
        instance, _ = build(cost=None)
        #
        assert instance.usage_estimate_micro("mystery", 1000, 400) == 0

    def test_a_missing_model_name_never_reaches_the_catalog(self):
        instance, calls = build(cost=1.0)
        #
        assert instance.usage_estimate_micro(None, 1000, 400) == 0
        assert calls == []

    def test_an_unreachable_catalog_reserves_nothing(self):
        instance, _ = build(raises=True)
        #
        assert instance.usage_estimate_micro("gpt-5", 1000, 400) == 0

    def test_the_default_ceiling_applies_when_unconfigured(self):
        instance, _ = build(cost=99.0)
        #
        expected = int(estimate.DEFAULT_MAX_CALL_COST_USD * 1_000_000)
        assert instance.usage_estimate_micro("gpt-5", 1000, 0) == expected

    def test_reports_the_configured_default_output_ceiling(self):
        instance, _ = build(reservation={"default_output_tokens": 128})
        #
        assert instance.usage_default_output_tokens() == 128

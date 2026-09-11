"""The two hooks: every call is recorded exactly once, and nothing buffers the body.

The hooks are the money path, so the assertions here are deliberately about the *row* that
lands rather than about internal state: an unmetered call must become a visible `unparsed` row
instead of nothing, which is the defect this replaces.
"""
import inspect
import types

import pytest

from usage import hooks
from usage.sources import registry


BEGIN_PARAMS = [
    "project_id", "user_id", "model_name", "endpoint", "headers", "provider", "run_id",
]

# Any uuid; what matters is that it survives canonicalisation and a malformed one does not
RUN_ID = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"

OPENAI_JSON = (
    b'{"model": "gpt-4o", "usage": {"prompt_tokens": 100, "completion_tokens": 20}}'
)
OPENAI_SSE_WITH_USAGE = (
    b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
    b'data: {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}\n\n'
    b'data: [DONE]\n\n'
)
OPENAI_SSE_WITHOUT_USAGE = (
    b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
    b'data: [DONE]\n\n'
)


class Recorder:
    """Stands in for the module: collects the rows the hooks ask to be written."""

    def __init__(self):
        self.rows = []

    def usage_write_event(self, row):
        self.rows.append(row)
        return True

    def usage_resolve_project_id(self, user_id, user_name, headers):  # pylint: disable=W0613
        return 7


class Prices:
    """Minimal costs_compute_llm_cost stand-in; `cost=None` is the unpriced model."""

    def __init__(self, cost=0.0005):
        self.cost = cost
        self.calls = []

    def timeout(self, seconds):  # pylint: disable=W0613
        return self

    def costs_compute_llm_cost(self, **kwargs):
        self.calls.append(kwargs)
        #
        if self.cost is None:
            return {"cost": None, "cost_source": None}
        #
        return {"cost": self.cost, "cost_source": "catalog"}


@pytest.fixture()
def metering(monkeypatch):
    """Metering on, with the writer and the pricing RPC captured."""
    registry.clear()
    registry.register_defaults()
    #
    recorder = Recorder()
    prices = Prices()
    #
    monkeypatch.setattr(hooks, "this", types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config={"usage": {"mode": "observe"}}),
        module=recorder,
    ))
    monkeypatch.setattr(hooks, "context", types.SimpleNamespace(rpc_manager=prices))
    #
    return types.SimpleNamespace(rows=recorder.rows, prices=prices, recorder=recorder)


@pytest.fixture()
def metering_off(monkeypatch):
    monkeypatch.setattr(hooks, "this", types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config={"usage": {"mode": "off"}}), module=None,
    ))


def begin(provider=None, model_name="gpt-4o", endpoint="/v1/chat/completions", headers=None):
    return hooks.begin_llm_call(
        project_id=7, user_id=42, model_name=model_name, endpoint=endpoint,
        headers={} if headers is None else headers, provider=provider,
    )


def drain(ctx, chunks, status=200, content_type="application/json"):
    response = {"status_code": status, "headers": {"Content-Type": content_type}}
    #
    return list(hooks.meter_llm_response(ctx, response, iter(chunks)))


class TestTheFrozenContract:
    def test_begin_signature_is_the_contract(self):
        # An interface plugin written against this must keep compiling; a rename has to
        # break here rather than at runtime in someone else's route.
        assert list(inspect.signature(hooks.begin_llm_call).parameters) == BEGIN_PARAMS

    def test_only_the_late_additions_are_optional(self):
        parameters = inspect.signature(hooks.begin_llm_call).parameters
        #
        # Optional so a second interface can adopt the hooks before it can supply either.
        assert parameters["provider"].default is None
        assert parameters["run_id"].default is None
        assert all(
            p.default is inspect.Parameter.empty
            for name, p in parameters.items() if name not in ("provider", "run_id")
        )

    def test_no_var_kwargs_so_a_typo_is_caught(self):
        kinds = [p.kind for p in inspect.signature(hooks.begin_llm_call).parameters.values()]
        #
        assert inspect.Parameter.VAR_KEYWORD not in kinds

    def test_meter_signature_is_the_contract(self):
        assert list(inspect.signature(hooks.meter_llm_response).parameters) \
            == ["ctx", "response", "iterator"]

    def test_unexpected_kwarg_raises_rather_than_being_swallowed(self, metering):
        with pytest.raises(TypeError):
            hooks.begin_llm_call(
                project_id=7, user_id=42, model_name="gpt-4o",
                endpoint="/v1/chat/completions", headers={}, tenant_id=1,
            )


class TestModeOff:
    def test_begin_returns_none(self, metering_off):
        assert begin() is None

    def test_the_iterator_is_returned_by_identity(self, metering_off):
        iterator = iter([b"chunk"])
        #
        # Identity, not equality: mode=off must add no wrapper and no buffering at all.
        assert hooks.meter_llm_response(None, object(), iterator) is iterator

    def test_the_iterator_is_not_consumed(self, metering_off):
        iterator = iter([b"a", b"b"])
        hooks.meter_llm_response(None, object(), iterator)
        #
        assert list(iterator) == [b"a", b"b"]


class TestPassthrough:
    def test_chunks_arrive_unchanged_and_in_order(self, metering):
        chunks = [b'{"usage": {"prompt_tokens": 1,', b' "completion_tokens": 2}}']
        #
        assert drain(begin(), chunks) == chunks

    def test_nothing_is_yielded_before_the_upstream_yields(self, metering):
        # The generator must not pull ahead: a lazy relay is what keeps first-byte latency.
        pulled = []

        def source():
            for chunk in (b"a", b"b"):
                pulled.append(chunk)
                yield chunk

        served = hooks.meter_llm_response(
            begin(), {"status_code": 200, "headers": {}}, source(),
        )
        #
        assert pulled == []
        assert next(served) == b"a"
        assert pulled == [b"a"]


class TestTheRowThatLands:
    def test_provider_reported_usage(self, metering):
        drain(begin(), [OPENAI_JSON])
        #
        row, = metering.rows
        assert row["token_source"] == "provider"
        assert (row["input_tokens"], row["output_tokens"]) == (100, 20)
        assert row["billable_input_tokens"] == 100
        assert row["dialect"] == "openai.chat"
        assert row["is_error"] is False

    def test_exactly_one_row_per_call(self, metering):
        drain(begin(), [OPENAI_JSON])
        drain(begin(), [OPENAI_JSON])
        #
        assert len(metering.rows) == 2
        assert len({row["idempotency_key"] for row in metering.rows}) == 2

    def test_an_upstream_error_is_recorded_and_still_relayed(self, metering):
        body = [b'{"error": {"message": "nope"}}']
        #
        assert drain(begin(), body, status=500) == body
        #
        row, = metering.rows
        assert row["is_error"] is True
        assert row["token_source"] == "unparsed"

    def test_tokens_reported_alongside_an_error_are_still_billed(self, metering):
        # A provider that rejects late has already charged for the prompt; skipping the body
        # on status alone turns that spend into a silent zero.
        body = [
            b'{"error": {"message": "context length"},'
            b' "usage": {"prompt_tokens": 11, "completion_tokens": 0}}'
        ]
        #
        drain(begin(), body, status=400)
        #
        row, = metering.rows
        assert row["is_error"] is True
        assert row["token_source"] == "provider"
        assert row["input_tokens"] == 11

    def test_no_dialect_still_writes_a_row(self, metering):
        # The defect this replaces returned early here, so the call vanished from billing.
        drain(begin(endpoint="/v1/audio/speech"), [b"\x00\x01"], content_type="audio/mpeg")
        #
        row, = metering.rows
        assert row["token_source"] == "unparsed"
        assert row["dialect"] is None

    def test_a_stream_without_a_usage_frame_is_unparsed(self, metering):
        # Exactly what a missing stream_options.include_usage looks like on the wire.
        drain(
            begin(), [OPENAI_SSE_WITHOUT_USAGE], content_type="text/event-stream",
        )
        #
        row, = metering.rows
        assert row["token_source"] == "unparsed"
        assert row["dialect"] == "openai.chat"

    def test_a_stream_with_a_usage_frame_is_provider_reported(self, metering):
        drain(begin(), [OPENAI_SSE_WITH_USAGE], content_type="text/event-stream")
        #
        row, = metering.rows
        assert row["token_source"] == "provider"
        assert (row["input_tokens"], row["output_tokens"]) == (7, 3)

    def test_the_run_id_header_is_carried_onto_the_row(self, metering):
        drain(begin(headers={"X-Elitea-Run-Id": RUN_ID}), [OPENAI_JSON])
        #
        assert metering.rows[0]["run_id"] == RUN_ID

    def test_an_explicitly_passed_run_id_wins_over_the_header(self, metering):
        # The interface canonicalises and parks the id; by metering time the header is gone
        # from the outbound request, so the parked value is the one that must be believed.
        ctx = hooks.begin_llm_call(
            project_id=7, user_id=42, model_name="gpt-4o", endpoint="/v1/chat/completions",
            headers={"X-Elitea-Run-Id": "0dc0d1e6-0000-4000-8000-000000000000"}, run_id=RUN_ID,
        )
        drain(ctx, [OPENAI_JSON])
        #
        assert metering.rows[0]["run_id"] == RUN_ID

    def test_a_malformed_run_id_is_dropped_rather_than_failing_the_insert(self, metering):
        # run_id is a Postgres uuid column: an unparseable value would abort the row.
        drain(begin(headers={"X-Elitea-Run-Id": "run-9"}), [OPENAI_JSON])
        #
        assert metering.rows[0]["run_id"] is None

    def test_an_unhyphenated_run_id_is_canonicalised(self, metering):
        drain(begin(headers={"X-Elitea-Run-Id": RUN_ID.replace("-", "")}), [OPENAI_JSON])
        #
        assert metering.rows[0]["run_id"] == RUN_ID

    def test_a_client_disconnect_mid_stream_is_still_billed(self, metering):
        served = hooks.meter_llm_response(
            begin(), {"status_code": 200, "headers": {}}, iter([OPENAI_JSON, b"tail"]),
        )
        next(served)
        served.close()
        #
        row, = metering.rows
        assert row["token_source"] == "provider"
        assert row["input_tokens"] == 100


class TestProviderNarrowsTheDialect:
    """The provider is a matching input; the dialect it selects is what the row records."""

    def test_the_provider_narrows_the_dialect(self, metering):
        # The live bug: a DIAL api_base with no `dial` marker reads as openai without this.
        drain(begin(provider="ai_dial"), [OPENAI_JSON])
        #
        row, = metering.rows
        assert row["dialect"] == "ai_dial.chat"

    def test_no_provider_leaves_todays_labels_alone(self, metering):
        drain(begin(), [OPENAI_JSON])
        #
        row, = metering.rows
        assert row["dialect"] == "openai.chat"


class TestPricing:
    def test_the_raw_model_name_is_priced(self, metering):
        # LiteLLM rewrites the name downstream; the costs catalog only knows the raw one.
        drain(begin(model_name="gpt-4o"), [OPENAI_JSON])
        #
        assert metering.prices.calls[0]["model_name"] == "gpt-4o"

    def test_billable_input_is_what_gets_priced(self, metering):
        drain(begin(), [OPENAI_JSON])
        #
        assert metering.prices.calls[0]["input_tokens"] == 100

    def test_cost_is_integer_micro_dollars(self, metering):
        drain(begin(), [OPENAI_JSON])
        #
        assert metering.rows[0]["cost_micro_usd"] == 500
        assert isinstance(metering.rows[0]["cost_micro_usd"], int)

    def test_an_unpriced_model_is_marked_never_a_silent_zero(self, metering):
        metering.prices.cost = None
        drain(begin(model_name="who-knows"), [OPENAI_JSON])
        #
        row, = metering.rows
        assert row["cost_source"] == "unpriced"
        assert row["cost_micro_usd"] == 0

    def test_a_pricing_failure_does_not_lose_the_row(self, metering, monkeypatch):
        def explode(**kwargs):
            raise RuntimeError("catalog down")

        monkeypatch.setattr(metering.prices, "costs_compute_llm_cost", explode)
        drain(begin(), [OPENAI_JSON])
        #
        assert metering.rows[0]["cost_source"] == "unpriced"
        assert metering.rows[0]["token_source"] == "provider"


class TestCacheConventions:
    def test_inclusive_and_exclusive_agree_on_billable_input(self, metering):
        # OpenAI counts cached tokens inside prompt_tokens, Anthropic reports them beside it.
        # Same true spend must bill the same, or one provider is silently over-charged.
        openai_body = (
            b'{"usage": {"prompt_tokens": 100, "completion_tokens": 5,'
            b' "prompt_tokens_details": {"cached_tokens": 40}}}'
        )
        anthropic_body = (
            b'{"usage": {"input_tokens": 60, "output_tokens": 5,'
            b' "cache_read_input_tokens": 40}}'
        )
        #
        drain(begin(), [openai_body])
        drain(begin(endpoint="/v1/messages"), [anthropic_body])
        #
        inclusive, exclusive = metering.rows
        assert inclusive["billable_input_tokens"] == exclusive["billable_input_tokens"] == 60
        assert inclusive["cache_read_tokens"] == exclusive["cache_read_tokens"] == 40


class TestFailureIsolation:
    def test_a_writer_failure_does_not_break_the_response(self, metering, monkeypatch):
        def explode(row):
            raise RuntimeError("db down")

        monkeypatch.setattr(metering.recorder, "usage_write_event", explode)
        #
        assert drain(begin(), [OPENAI_JSON]) == [OPENAI_JSON]

    def test_an_unresolved_project_is_not_written(self, metering, monkeypatch):
        monkeypatch.setattr(
            metering.recorder, "usage_resolve_project_id", lambda *a, **k: None,
        )
        ctx = hooks.begin_llm_call(
            project_id=None, user_id=42, model_name="gpt-4o",
            endpoint="/v1/chat/completions", headers={},
        )
        #
        assert drain(ctx, [OPENAI_JSON]) == [OPENAI_JSON]
        assert metering.rows == []

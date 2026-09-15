"""The surface a runtime interface calls: what it may decide, and what it may not.

Everything here used to live in `runtime_interface_litellm`, which meant every future
interface would have had to re-implement it — and one of them got it wrong in a way that let a
caller opt out of billing. These tests pin the two properties that keeps safe: the decision to
record comes from this plugin's own mode and from nothing a caller sends, and the provider
lookup does not turn into a Postgres query per LLM call.
"""
import inspect
import types

import pytest

from usage import hooks, interface


PROVIDER = "ai_dial"

RUN_ID = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"

# Opaque on this side: the header is decoded and validated in hooks, not here
ATTRIBUTION_BLOB = "eyJlbnRpdHlfaWQiOjF9"

OPENAI_JSON = (
    b'{"model": "gpt-4o", "usage": {"prompt_tokens": 100, "completion_tokens": 20}}'
)


class RecordingRpc:
    """Counts provider lookups so a cache hit is distinguishable from a repeat query."""

    def __init__(self, provider=PROVIDER, explode=False):
        self.provider = provider
        self.explode = explode
        self.calls = []

    def timeout(self, _seconds):
        return self

    def configurations_get_model_provider(self, project_id, model_name):
        self.calls.append((project_id, model_name))
        #
        if self.explode:
            raise RuntimeError("configurations down")
        #
        return self.provider


@pytest.fixture()
def rpc(monkeypatch):
    """A recording provider lookup, with the cache emptied so counts mean something."""
    interface._provider_cache.clear()  # pylint: disable=W0212
    recorder = RecordingRpc()
    monkeypatch.setattr(interface, "context", types.SimpleNamespace(rpc_manager=recorder))
    #
    return recorder


@pytest.fixture()
def mode_observe(monkeypatch):
    # interface has its own `this`, and prepare_llm_call reads the reservation default off it
    module = types.SimpleNamespace(usage_default_output_tokens=lambda: 4096)
    monkeypatch.setattr(hooks, "this", types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config={"usage": {"mode": "observe"}}), module=module,
    ))
    monkeypatch.setattr(interface, "this", types.SimpleNamespace(module=module))


@pytest.fixture()
def mode_off(monkeypatch):
    monkeypatch.setattr(hooks, "this", types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config={"usage": {"mode": "off"}}), module=None,
    ))


def target(endpoint="/v1/chat/completions", body=None, headers=None):
    return {
        "endpoint": endpoint,
        "json": {"model": "gpt-4o"} if body is None else body,
        "headers": {} if headers is None else headers,
    }


def auth(**extra):
    parked = {"project_id": 7, "user": {"id": 42}}
    parked.update(extra)
    #
    return parked


class TestTheFrozenContract:
    """The interface plugins call these positionally, across separately-versioned repos."""

    def test_prepare_signature(self):
        assert list(inspect.signature(interface.prepare_llm_call).parameters) \
            == ["proxy_target", "proxy_auth", "raw_model_name", "model_project_id"]

    def test_meter_signature(self):
        assert list(inspect.signature(interface.meter_llm_call).parameters) \
            == ["proxy_target", "proxy_auth", "response", "iterator"]

    def test_the_model_facts_are_optional(self):
        parameters = inspect.signature(interface.prepare_llm_call).parameters
        #
        # A body with no `model` is still a call that must produce a row.
        assert parameters["raw_model_name"].default is None
        assert parameters["model_project_id"].default is None


class TestOnlyTheModeDecides:
    def test_a_prepared_call_is_metered(self, rpc, mode_observe):
        proxy_auth = auth()
        interface.prepare_llm_call(target(), proxy_auth, "gpt-4o", 7)
        #
        assert interface.RAW_MODEL_AUTH_KEY in proxy_auth

    def test_mode_off_parks_nothing(self, rpc, mode_off):
        proxy_auth = auth()
        interface.prepare_llm_call(target(), proxy_auth, "gpt-4o", 7)
        #
        assert proxy_auth == auth()

    def test_mode_off_returns_the_iterator_by_identity(self, mode_off):
        iterator = iter([b"chunk"])
        #
        assert interface.meter_llm_call(target(), auth(), object(), iterator) is iterator

    def test_an_unprepared_call_is_not_metered(self, mode_observe):
        # Belt and braces: mode could have flipped on between prepare and meter, and half a
        # metered call (no provider, no parked model) must not be invented out of nothing.
        iterator = iter([b"chunk"])
        #
        assert interface.meter_llm_call(target(), auth(), object(), iterator) is iterator

    @pytest.mark.parametrize("header", ["X-Elitea-Audited", "x-elitea-audited"])
    def test_no_caller_header_can_suppress_metering(self, rpc, mode_observe, header):
        # The defect this replaces: a caller-controlled header skipped the ledger entirely,
        # which is a free-LLM-calls bug, not a double-counting nicety.
        proxy_auth = auth()
        interface.prepare_llm_call(
            target(headers={header: "true"}), proxy_auth, "gpt-4o", 7,
        )
        #
        assert interface.RAW_MODEL_AUTH_KEY in proxy_auth


class TestTheParkedRunFacts:
    """Which run, which conversation: parked on proxy_auth by the interface, read back here.

    The interface strips X-Elitea-Run-Id and X-Elitea-Attribution before the request leaves —
    those ids are ours, not the upstream's — so the parked values are metering's only source.
    """

    @pytest.fixture()
    def began(self, monkeypatch, mode_observe):  # pylint: disable=W0613
        """The kwargs each metered call hands to the hook."""
        recorded = []
        monkeypatch.setattr(hooks, "begin_llm_call", lambda **kwargs: recorded.append(kwargs))
        #
        return recorded

    def meter(self, proxy_auth):
        interface.prepare_llm_call(target(), proxy_auth, "gpt-4o", 7)
        interface.meter_llm_call(target(), proxy_auth, object(), iter([OPENAI_JSON]))

    def test_both_reach_the_hook(self, rpc, began):
        self.meter(auth(**{
            interface.RUN_ID_AUTH_KEY: RUN_ID,
            interface.ATTRIBUTION_AUTH_KEY: ATTRIBUTION_BLOB,
        }))
        #
        assert began[0]["run_id"] == RUN_ID
        assert began[0]["attribution"] == ATTRIBUTION_BLOB

    def test_an_interface_that_parks_neither_still_meters(self, rpc, began):
        # Both are optional: an interface can adopt metering before it can supply either.
        self.meter(auth())
        #
        assert (began[0]["run_id"], began[0]["attribution"]) == (None, None)


class TestTheUsageFrame:
    """Streamed OpenAI-family calls report no tokens unless include_usage is asked for."""

    def test_it_is_injected_for_a_streamed_chat_call(self, rpc, mode_observe):
        body = {"model": "gpt-4o", "stream": True}
        interface.prepare_llm_call(target(body=body), auth(), "gpt-4o", 7)
        #
        assert body["stream_options"] == {"include_usage": True}

    def test_a_caller_opting_out_is_overridden(self, rpc, mode_observe):
        # Not a default: `include_usage: false` would otherwise be an unbilled call.
        body = {"model": "gpt-4o", "stream": True, "stream_options": {"include_usage": False}}
        interface.prepare_llm_call(target(body=body), auth(), "gpt-4o", 7)
        #
        assert body["stream_options"]["include_usage"] is True

    def test_an_empty_options_dict_is_filled_in(self, rpc, mode_observe):
        body = {"model": "gpt-4o", "stream": True, "stream_options": {}}
        interface.prepare_llm_call(target(body=body), auth(), "gpt-4o", 7)
        #
        assert body["stream_options"]["include_usage"] is True

    def test_other_stream_options_are_kept(self, rpc, mode_observe):
        body = {
            "model": "gpt-4o", "stream": True,
            "stream_options": {"continuous_usage_stats": True},
        }
        interface.prepare_llm_call(target(body=body), auth(), "gpt-4o", 7)
        #
        assert body["stream_options"] == {
            "continuous_usage_stats": True, "include_usage": True,
        }

    @pytest.mark.parametrize("endpoint", [
        "/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21",
        "/v1/chat/completions?foo=bar",
    ])
    def test_a_query_string_does_not_defeat_the_check(self, rpc, mode_observe, endpoint):
        # Azure and DIAL carry an api-version query; matching on the raw string would skip them.
        body = {"model": "gpt-4o", "stream": True}
        interface.prepare_llm_call(target(endpoint=endpoint, body=body), auth(), "gpt-4o", 7)
        #
        assert body["stream_options"] == {"include_usage": True}

    @pytest.mark.parametrize("endpoint", [
        "/v1/messages", "/v1/embeddings", "/v1/responses", "/v1/images/generations",
        "/model/anthropic.claude-sonnet-4/converse-stream",
        "/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse",
        "/api/chat",
    ])
    def test_it_is_not_injected_where_the_provider_would_reject_it(
            self, rpc, mode_observe, endpoint,
    ):
        # include_usage gates streamed usage on chat/legacy completions alone. Responses,
        # messages, converse, Google and Ollama always report it and do not take the field,
        # so sending it there is a needless 400 risk on a live call.
        body = {"model": "gpt-4o", "stream": True}
        interface.prepare_llm_call(target(endpoint=endpoint, body=body), auth(), "gpt-4o", 7)
        #
        assert "stream_options" not in body

    def test_a_non_streamed_body_is_untouched(self, rpc, mode_observe):
        body = {"model": "gpt-4o"}
        interface.prepare_llm_call(target(body=body), auth(), "gpt-4o", 7)
        #
        assert body == {"model": "gpt-4o"}

    def test_mode_off_leaves_the_wire_byte_identical(self, rpc, mode_off):
        body = {"model": "gpt-4o", "stream": True}
        interface.prepare_llm_call(target(body=body), auth(), "gpt-4o", 7)
        #
        assert body == {"model": "gpt-4o", "stream": True}

    def test_a_form_data_body_does_not_raise(self, rpc, mode_observe):
        proxy_target = target(body=None)
        proxy_target["json"] = None
        #
        interface.prepare_llm_call(proxy_target, auth(), "dall-e-3", 7)


class TestResolveProvider:
    """The lookup runs a Postgres query, so the cache is the only thing between metering
    and one query per LLM call."""

    def test_first_lookup_queries_and_returns_the_provider(self, rpc):
        assert interface.resolve_provider(3, "gpt-4o") == PROVIDER
        assert rpc.calls == [(3, "gpt-4o")]

    def test_a_repeat_lookup_is_served_from_the_cache(self, rpc):
        interface.resolve_provider(3, "gpt-4o")
        interface.resolve_provider(3, "gpt-4o")
        #
        assert len(rpc.calls) == 1

    def test_the_key_is_project_and_model(self, rpc):
        interface.resolve_provider(3, "gpt-4o")
        interface.resolve_provider(4, "gpt-4o")
        interface.resolve_provider(3, "claude")
        #
        assert len(rpc.calls) == 3

    def test_an_unknown_model_caches_the_absence(self, rpc):
        # None is a legitimate answer for an externally-managed model, and re-asking for it
        # every call is exactly the DB load this cache exists to prevent.
        rpc.provider = None
        #
        assert interface.resolve_provider(3, "who-knows") is None
        assert interface.resolve_provider(3, "who-knows") is None
        assert len(rpc.calls) == 1

    def test_a_failed_lookup_degrades_to_none_without_raising(self, rpc):
        rpc.explode = True
        #
        assert interface.resolve_provider(3, "gpt-4o") is None

    def test_a_failed_lookup_is_not_remembered(self, rpc):
        rpc.explode = True
        interface.resolve_provider(3, "gpt-4o")
        #
        rpc.explode = False
        assert interface.resolve_provider(3, "gpt-4o") == PROVIDER

    def test_nothing_is_asked_for_an_unknown_model_name(self, rpc):
        assert interface.resolve_provider(3, None) is None
        assert interface.resolve_provider(None, "gpt-4o") is None
        assert rpc.calls == []

    def test_the_cache_expires(self):
        cache = interface._provider_cache  # pylint: disable=W0212
        #
        assert cache.maxsize > 0
        assert cache.ttl == 60


class TestTheProviderScope:
    """A private model must not label a same-named public one, or vice versa."""

    def test_the_scope_the_model_resolved_in_is_what_is_asked_about(self, rpc, mode_observe):
        proxy_auth = auth()
        # project_id is the caller's; model_project_id is where LiteLLM found the model
        interface.prepare_llm_call(target(), proxy_auth, "gpt-4o", 1)
        #
        assert rpc.calls == [(1, "gpt-4o")]
        assert proxy_auth[interface.PROVIDER_AUTH_KEY] == PROVIDER


class TestFailureIsolation:
    def test_a_broken_lookup_still_prepares_the_call(self, rpc, mode_observe):
        rpc.explode = True
        proxy_auth = auth()
        #
        interface.prepare_llm_call(target(), proxy_auth, "gpt-4o", 7)
        #
        assert proxy_auth[interface.PROVIDER_AUTH_KEY] is None
        assert proxy_auth[interface.RAW_MODEL_AUTH_KEY] == "gpt-4o"

    def test_an_unreadable_mode_does_not_raise(self, rpc, monkeypatch):
        monkeypatch.setattr(hooks, "this", None)
        #
        interface.prepare_llm_call(target(), auth(), "gpt-4o", 7)

    def test_a_metering_failure_does_not_cost_the_response(self, rpc, mode_observe, monkeypatch):
        def explode(**kwargs):
            raise RuntimeError("metering down")

        monkeypatch.setattr(hooks, "begin_llm_call", explode)
        #
        proxy_auth = auth()
        interface.prepare_llm_call(target(), proxy_auth, "gpt-4o", 7)
        iterator = iter([OPENAI_JSON])
        #
        assert interface.meter_llm_call(target(), proxy_auth, object(), iterator) is iterator

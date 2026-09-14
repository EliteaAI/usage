"""The whole gateway path, with only the DB and the two RPCs stubbed.

The unit tests cover each half in isolation. This one replays what a runtime interface actually
does — hand over the proxy dicts, then hand back the response iterator — and asserts on the row
that lands, because that row is the billing record. It is also the regression guard for the two
defects that mattered most: a caller header used to skip the ledger, and an ordinary platform
predict used to be excluded from it.
"""
import types

import pytest

from usage import hooks, interface
from usage.sources import registry


RUN_ID = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"

# A DIAL body is indistinguishable from an OpenAI one; only the credential says otherwise
DIAL_JSON = (
    b'{"model": "gpt-4o", "usage": {"prompt_tokens": 100, "completion_tokens": 20}}'
)


class Recorder:
    def __init__(self):
        self.rows = []

    def usage_write_event(self, row):
        self.rows.append(row)
        return True

    def usage_resolve_project_id(self, user_id, user_name, headers):  # pylint: disable=W0613
        return 7


class Prices:
    def timeout(self, seconds):  # pylint: disable=W0613
        return self

    def costs_compute_llm_cost(self, **kwargs):  # pylint: disable=W0613
        return {"cost": 0.0005, "cost_source": "catalog"}


class ProviderLookup:
    def __init__(self, provider="ai_dial"):
        self.provider = provider

    def timeout(self, seconds):  # pylint: disable=W0613
        return self

    def configurations_get_model_provider(self, project_id, model_name):  # pylint: disable=W0613
        return self.provider


def _gateway(monkeypatch, mode):
    registry.clear()
    registry.register_defaults()
    interface._provider_cache.clear()  # pylint: disable=W0212
    #
    recorder = Recorder()
    monkeypatch.setattr(hooks, "this", types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config={"usage": {"mode": mode}}), module=recorder,
    ))
    monkeypatch.setattr(hooks, "context", types.SimpleNamespace(rpc_manager=Prices()))
    monkeypatch.setattr(
        interface, "context", types.SimpleNamespace(rpc_manager=ProviderLookup()),
    )
    #
    return recorder


@pytest.fixture()
def gateway(monkeypatch):
    return _gateway(monkeypatch, "observe")


@pytest.fixture()
def gateway_off(monkeypatch):
    return _gateway(monkeypatch, "off")


def relay(headers=None, body=None, chunks=(DIAL_JSON,), stream=False, run_id=RUN_ID):
    """One LLM call through the gateway, exactly as the interface plugin drives it."""
    proxy_target = {
        "endpoint": "/v1/chat/completions",
        "headers": {} if headers is None else dict(headers),
        "json": {"model": "gpt-4o"} if body is None else body,
    }
    if stream:
        proxy_target["json"]["stream"] = True
    #
    proxy_auth = {"project_id": 7, "user": {"id": 42, "name": "admin"}}
    if run_id is not None:
        proxy_auth["platform_run_id"] = run_id
    #
    interface.prepare_llm_call(proxy_target, proxy_auth, "gpt-4o", 1)
    #
    response = {"status_code": 200, "headers": {"Content-Type": "application/json"}}
    served = list(
        interface.meter_llm_call(proxy_target, proxy_auth, response, iter(chunks)),
    )
    #
    return proxy_target, served


class TestTheRowThatLands:
    def test_one_row_labelled_by_the_credential_family(self, gateway):
        relay()
        #
        row, = gateway.rows
        # The live defect: a DIAL api_base has no `dial` marker, so sniffing alone reads
        # every such call as openai.chat forever. The provider is never stored — the dialect
        # it selected is the only thing the row keeps.
        assert row["dialect"] == "ai_dial.chat"
        assert "provider" not in row
        assert row["token_source"] == "provider"
        assert (row["input_tokens"], row["output_tokens"]) == (100, 20)

    def test_the_run_id_parked_by_the_interface_reaches_the_row(self, gateway):
        relay()
        #
        assert gateway.rows[0]["run_id"] == RUN_ID

    def test_a_call_with_no_run_id_is_still_billed(self, gateway):
        relay(run_id=None)
        #
        row, = gateway.rows
        assert row["run_id"] is None
        assert row["token_source"] == "provider"

    def test_the_body_reaches_the_client_unchanged(self, gateway):
        _, served = relay()
        #
        assert served == [DIAL_JSON]


class TestPlatformTrafficIsNotExcluded:
    """Chat, agent, pipeline, evaluation and MCP predicts all send X-Elitea-Audited."""

    def test_an_audited_predict_is_still_written_to_the_ledger(self, gateway):
        # That header exists to stop two tracing spans counting the same call. It said
        # nothing about billing, and using it here excluded nearly all real traffic.
        relay(headers={"X-Elitea-Audited": "1"})
        #
        assert len(gateway.rows) == 1

    def test_a_caller_cannot_forge_its_way_out_of_billing(self, gateway):
        # An external PAT client can set any header it likes.
        relay(headers={"X-Elitea-Audited": "true"})
        #
        assert len(gateway.rows) == 1


class TestStreamedCalls:
    def test_the_usage_frame_is_requested_and_then_read(self, gateway):
        sse = (
            b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
            b'data: {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}\n\n'
            b'data: [DONE]\n\n'
        )
        proxy_target = {
            "endpoint": "/v1/chat/completions", "headers": {},
            "json": {"model": "gpt-4o", "stream": True},
        }
        proxy_auth = {"project_id": 7, "user": {"id": 42, "name": "admin"}}
        #
        interface.prepare_llm_call(proxy_target, proxy_auth, "gpt-4o", 1)
        assert proxy_target["json"]["stream_options"] == {"include_usage": True}
        #
        response = {"status_code": 200, "headers": {"Content-Type": "text/event-stream"}}
        list(interface.meter_llm_call(proxy_target, proxy_auth, response, iter([sse])))
        #
        row, = gateway.rows
        assert row["token_source"] == "provider"
        assert (row["input_tokens"], row["output_tokens"]) == (7, 3)


class TestModeOff:
    def test_nothing_is_written(self, gateway_off):
        relay()
        #
        assert gateway_off.rows == []

    def test_the_wire_is_byte_identical_to_today(self, gateway_off):
        proxy_target, served = relay(stream=True)
        #
        assert proxy_target["json"] == {"model": "gpt-4o", "stream": True}
        assert served == [DIAL_JSON]

"""#6699 — `_admit`'s fail-closed branch: the gate must not silently under-record.

`_write_path_healthy()` calls `this.module.usage_write_path_healthy()`; an older module lacking
that method (or the AttributeError this file exercises) is already treated as healthy by the
bare except in `hooks._write_path_healthy`, which is why every pre-existing hooks test — none of
which stub the method — still exercises the ordinary `_acquire` path unchanged.
"""
import json
import types

import pytest

from usage import hooks

OPENAI_JSON = (
    b'{"model": "gpt-4o", "usage": {"prompt_tokens": 100, "completion_tokens": 20}}'
)


class Recorder:
    """Stands in for the module: a configurable write-path health plus the usual gate calls."""

    def __init__(self, healthy=True):
        self.rows = []
        self.healthy = healthy
        self.acquire_calls = 0

    def usage_write_path_healthy(self):
        if isinstance(self.healthy, Exception):
            raise self.healthy
        #
        return self.healthy

    def usage_write_event(self, row):
        self.rows.append(row)
        return True

    def usage_enqueue_event(self, row):  # pylint: disable=W0613
        return False

    def usage_estimate_micro(self, model_name, max_output_tokens, input_size_bytes):  # pylint: disable=W0613
        return 0

    def usage_gate_acquire(self, project_id, user_id, estimate_micro, moment):  # pylint: disable=W0613
        self.acquire_calls += 1
        return {"allowed": True, "scope": None, "reservation": None, "healthy": True}

    def usage_gate_settle(self, reservation, actual_micro):  # pylint: disable=W0613
        return True

    def usage_resolve_project_id(self, user_id, user_name, headers):  # pylint: disable=W0613
        return 7


class Prices:
    def timeout(self, seconds):  # pylint: disable=W0613
        return self

    def costs_compute_llm_cost(self, **kwargs):  # pylint: disable=W0613
        return {"cost": 0.0005, "cost_source": "catalog"}


def build(monkeypatch, mode, healthy):
    recorder = Recorder(healthy=healthy)
    #
    monkeypatch.setattr(hooks, "this", types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config={"usage": {"mode": mode}}),
        module=recorder,
    ))
    monkeypatch.setattr(hooks, "context", types.SimpleNamespace(rpc_manager=Prices()))
    #
    return recorder


def begin():
    return hooks.begin_llm_call(
        project_id=7, user_id=42, model_name="gpt-4o", endpoint="/v1/chat/completions",
        headers={},
    )


class TestEnforceUnhealthy:
    def test_the_call_is_denied_before_the_gate_is_ever_reached(self, monkeypatch):
        recorder = build(monkeypatch, "enforce", healthy=False)
        #
        ctx = begin()
        #
        assert ctx.denied is True
        assert recorder.acquire_calls == 0

    def test_the_response_is_a_503_usage_unavailable_carrying_the_new_code(self, monkeypatch):
        build(monkeypatch, "enforce", healthy=False)
        #
        body, status, headers = begin().response
        #
        assert status == 503
        assert headers["Content-Type"] == "application/json"
        payload = json.loads(body)
        assert payload["error"]["type"] == "usage_unavailable"
        assert payload["error"]["code"] == "usage_write_path_unavailable"
        assert payload["error"]["message"] == hooks.GATE_UNHEALTHY_MESSAGE

    def test_it_is_not_the_429_budget_body(self, monkeypatch):
        build(monkeypatch, "enforce", healthy=False)
        #
        _, status, _ = begin().response
        #
        assert status != 429

    def test_a_probe_that_raises_is_treated_as_healthy_not_denied(self, monkeypatch):
        recorder = build(monkeypatch, "enforce", healthy=RuntimeError("catalog down"))
        #
        ctx = begin()
        #
        assert ctx.denied is False
        assert recorder.acquire_calls == 1


class TestObserveUnhealthy:
    def test_the_call_is_served_unrecorded_rather_than_denied(self, monkeypatch):
        recorder = build(monkeypatch, "observe", healthy=False)
        #
        ctx = begin()
        #
        assert ctx.denied is False
        assert ctx.response is None
        assert recorder.acquire_calls == 0

    def test_the_loss_is_logged_loudly(self, monkeypatch, recording_log):
        build(monkeypatch, "observe", healthy=False)
        #
        begin()
        #
        assert any(
            "write path is unhealthy" in message for message in recording_log.messages("error")
        )


class TestHealthy:
    def test_the_gate_is_reached_and_behaves_as_before(self, monkeypatch):
        recorder = build(monkeypatch, "enforce", healthy=True)
        #
        ctx = begin()
        #
        assert ctx.denied is False
        assert recorder.acquire_calls == 1
        assert ctx.reservation is None

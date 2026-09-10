#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Two-stage dispatch, per-response instances, and extensibility without core edits """

import pytest

from usage.sources import base, registry

PROXY_ENDPOINTS = [
    ("/v1/chat/completions", "openai.chat"),
    ("/v1/completions", "openai.chat"),
    ("/v1/responses", "openai.responses"),
    ("/v1/embeddings", "openai.embeddings"),
    ("/v1/messages", "anthropic.messages"),
]

ENGINE_ENDPOINTS = [
    ("/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21", "azure.chat"),
    ("/openai/deployments/gpt-4o/chat/completions", "ai_dial.chat"),
    ("/model/eu.amazon.nova-pro-v1:0/converse", "bedrock.converse"),
    ("/model/eu.amazon.nova-pro-v1:0/converse-stream", "bedrock.converse"),
    ("/model/anthropic.claude-3-5-sonnet-20241022-v2:0/invoke", "bedrock.invoke"),
    (
        "/model/anthropic.claude-3-5-sonnet-20241022-v2:0/invoke-with-response-stream",
        "bedrock.invoke",
    ),
    ("/v1beta/models/gemini-2.5-pro:generateContent", "google.generate_content"),
    ("/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse", "google.generate_content"),
    ("/api/chat", "ollama.native"),
    ("/api/generate", "ollama.native"),
]


class TestStageOneDispatch:
    """Endpoint plus content-type resolves nearly everything, with no body needed."""

    @pytest.mark.parametrize("endpoint,expected", PROXY_ENDPOINTS)
    def test_normalized_proxy_paths(self, registered_dialects, endpoint, expected):
        # These are the only five paths runtime_interface_litellm's proxy emits, so they
        # are the ones that matter most in production.
        dialect = registry.match(endpoint, "application/json")
        #
        assert dialect is not None
        assert dialect.id == expected

    @pytest.mark.parametrize("endpoint,expected", ENGINE_ENDPOINTS)
    def test_provider_native_paths(self, registered_dialects, endpoint, expected):
        dialect = registry.match(endpoint, "application/json")
        #
        assert dialect is not None
        assert dialect.id == expected

    def test_azure_needs_api_version_to_beat_dial(self, registered_dialects):
        # Azure and DIAL share the deployment path shape; only Azure carries api-version.
        # Both parse identically, so a mislabel is cosmetic, but the label feeds reporting.
        azure = registry.match(
            "/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21",
            "application/json",
        )
        dial = registry.match("/openai/deployments/gpt-4o/chat/completions", "application/json")
        #
        assert azure.id == "azure.chat"
        assert dial.id == "ai_dial.chat"


class TestStageTwoDispatch:
    """When the path says nothing, the body shape has to."""

    @pytest.mark.parametrize("head,expected", [
        (b'{"usage": {"prompt_tokens": 5}}', "openai.chat"),
        (b'{"usage": {"input_tokens": 5, "input_tokens_details": {}}}', "openai.responses"),
        (b'{"usage": {"input_tokens": 5, "cache_read_input_tokens": 0}}', "anthropic.messages"),
        (b'{"usageMetadata": {"promptTokenCount": 5}}', "google.generate_content"),
        (b'{"prompt_eval_count": 5, "eval_count": 1}', "ollama.native"),
    ])
    def test_body_signatures_are_mutually_exclusive(
        self, registered_dialects, head, expected,
    ):
        # An unrecognised route still has to be metered, so each family's signature key
        # must identify it on its own. These four key sets never co-occur on the wire.
        dialect = registry.match("/some/unknown/route", "application/json", head)
        #
        assert dialect is not None
        assert dialect.id == expected

    def test_stage_one_still_wins_when_head_is_supplied(self, registered_dialects):
        # A head that looks like Anthropic must not steal a request the path already
        # assigned to Responses; stage 1 runs to completion before any body probing.
        dialect = registry.match(
            "/v1/responses", "application/json", b'{"usage": {"input_tokens": 3}}',
        )
        #
        assert dialect.id == "openai.responses"

    def test_empty_head_does_not_skip_stage_one(self, registered_dialects):
        # Regression guard: b"" is interned, so an identity check against the head
        # argument once made an empty head skip stage 1 entirely.
        assert registry.match("/v1/chat/completions", "application/json", b"") is not None


class TestNoMatch:
    """Unrecognised responses must come back as None so the caller can flag them."""

    @pytest.mark.parametrize("endpoint,content_type,head", [
        ("/healthz", "text/plain", b"ok"),
        ("/v1/models", "application/json", b'{"data": []}'),
        ("", "", b""),
        (None, None, b""),
        ("/v1/audio/speech", "audio/mpeg", b"\x00\x01\x02"),
    ])
    def test_unknown_returns_none(self, registered_dialects, endpoint, content_type, head):
        assert registry.match(endpoint, content_type, head) is None


class TestInstanceIsolation:
    """A dialect is a per-response state machine, so match() must not share one."""

    def test_each_match_returns_a_fresh_instance(self, registered_dialects):
        first = registry.match("/v1/chat/completions", "application/json")
        second = registry.match("/v1/chat/completions", "application/json")
        #
        assert first is not second

    def test_state_does_not_leak_between_responses(self, registered_dialects):
        body = b'{"usage": {"prompt_tokens": 11, "completion_tokens": 2}}'
        #
        first = registry.match("/v1/chat/completions", "application/json")
        first.feed(body)
        assert first.result().input_tokens == 11
        #
        second = registry.match("/v1/chat/completions", "application/json")
        assert second.result().input_tokens is None

    def test_get_also_returns_fresh_instances(self, registered_dialects):
        assert registry.get("openai.chat") is not registry.get("openai.chat")

    def test_get_unknown_is_none(self, registered_dialects):
        assert registry.get("nope.nothing") is None


class TestExtensibility:
    """Acceptance criterion: a new dialect needs no edit under usage/sources/."""

    def test_a_test_local_dialect_registers_and_wins(self):
        class VendorXDialect:
            """Defined entirely inside this test file — nothing in the library knows it."""

            id = "vendor_x.chat"

            def __init__(self):
                self.reading = base.UsageReading(dialect=self.id)

            def matches(self, endpoint, content_type, head):
                return "/vendor-x/" in (endpoint or "")

            def feed(self, chunk):
                self.reading.input_tokens = len(chunk)

            def result(self):
                return self.reading

        registry.clear()
        try:
            registry.register(VendorXDialect)
            registry.register_defaults()
            #
            matched = registry.match("/vendor-x/generate", "application/json")
            assert matched is not None
            assert matched.id == "vendor_x.chat"
            assert isinstance(matched, base.UsageDialect)
            #
            # Registering an unknown dialect must not disturb the built-ins.
            assert registry.match("/v1/chat/completions", "application/json").id == "openai.chat"
            assert "vendor_x.chat" in registry.all()
        finally:
            registry.clear()
            registry.register_defaults()

    def test_all_lists_every_default_dialect(self, registered_dialects):
        assert set(registry.all()) == {
            "openai.chat", "openai.embeddings", "openai.responses", "azure.chat",
            "ai_dial.chat", "anthropic.messages", "bedrock.converse", "bedrock.invoke",
            "google.generate_content", "ollama.native",
        }


class TestFaultTolerance:
    """One broken dialect must not take the whole dispatch down."""

    def test_a_raising_matches_is_skipped(self):
        class ExplodingDialect:
            id = "exploding.dialect"

            def matches(self, endpoint, content_type, head):
                raise RuntimeError("boom")

            def feed(self, chunk):
                pass

            def result(self):
                return base.UsageReading()

        registry.clear()
        try:
            registry.register(ExplodingDialect)
            registry.register_defaults()
            #
            # The exploding dialect is registered first, so it is asked first. Dispatch
            # has to keep walking rather than propagate its error onto the money path.
            dialect = registry.match("/v1/chat/completions", "application/json")
            assert dialect is not None
            assert dialect.id == "openai.chat"
        finally:
            registry.clear()
            registry.register_defaults()

    def test_re_registering_the_same_id_does_not_duplicate(self, registered_dialects):
        before = list(registry.all())
        registry.register_defaults()
        #
        assert list(registry.all()) == before

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

from corpus import json_body

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


class TestProviderNarrowing:
    """The provider comes from the credential in use, so it is known where a URL is not.

    It narrows the candidate dialects; the endpoint and body still decide the shape. A provider
    that matches nothing falls back to sniffing everything, so narrowing can never be the reason
    a readable body goes unparsed.
    """

    def test_a_provider_picks_the_right_label_for_an_ambiguous_endpoint(self, registered_dialects):
        # Azure and DIAL speak the same body over the same deployment path; only the credential
        # tells them apart when the api-version marker is missing.
        matched = registry.match(
            "/openai/deployments/gpt-4o/chat/completions", "application/json", provider="azure_open_ai",
        )
        #
        assert matched.id == "azure.chat"

    def test_a_dial_credential_on_a_marker_less_endpoint_reads_as_dial(self, registered_dialects):
        # The live bug this change fixes: a DIAL api_base such as https://ai-proxy.lab.epam.com
        # carries no `dial` marker and no deployment path, so sniffing can only ever say openai.
        endpoint = "/v1/chat/completions"
        #
        assert registry.match(endpoint, "application/json").id == "openai.chat"
        assert registry.match(endpoint, "application/json", provider="ai_dial").id == "ai_dial.chat"

    def test_a_provider_without_a_dialect_for_this_shape_still_parses(self, registered_dialects):
        # DIAL serves embeddings too, and there is no ai_dial.embeddings — narrowing must not
        # turn a perfectly readable body into an unparsed one.
        matched = registry.match("/v1/embeddings", "application/json", provider="ai_dial")
        #
        assert matched.id == "openai.embeddings"

    def test_an_unregistered_provider_warns_and_sniffs(self, registered_dialects, monkeypatch):
        calls = []
        monkeypatch.setattr(registry.log, "warning", lambda msg, *a, **k: calls.append(msg % a))
        #
        matched = registry.match(
            "/v1/chat/completions", "application/json", provider="vendor_z",
        )
        #
        assert matched.id == "openai.chat"
        assert any("has no registered dialect" in call for call in calls)

    def test_no_provider_behaves_exactly_as_before(self, registered_dialects):
        assert registry.match("/v1/messages", "application/json").id == "anthropic.messages"

    def test_a_narrowed_match_still_binds_the_event_stream_framer(self, registered_dialects):
        bound = registry.get(
            "bedrock.converse",
            "/model/eu.amazon.nova-pro-v1:0/converse-stream",
            "application/vnd.amazon.eventstream",
        )
        matched = registry.match(
            "/model/eu.amazon.nova-pro-v1:0/converse-stream",
            "application/vnd.amazon.eventstream",
            provider="amazon_bedrock",
        )
        #
        assert matched.id == "bedrock.converse"
        assert type(matched._framer) is type(bound._framer)  # pylint: disable=W0212

    def test_the_reading_carries_the_narrowed_label(self, registered_dialects, fixtures):
        # The provider is an input to matching, not a fact of its own: what survives onto the
        # reading is the dialect it selected, whose prefix is that provider.
        matched = registry.match("/v1/chat/completions", "application/json", provider="ai_dial")
        matched.feed(json_body(fixtures("ai_dial_chat_json")["body"]))
        #
        assert matched.result().dialect == "ai_dial.chat"


class TestGetBindsTheInstance:
    def test_get_installs_the_event_stream_framer(self, registered_dialects):
        # Without the request context an AWS response would be read as text and report nothing.
        bound = registry.get(
            "bedrock.converse",
            "/model/eu.amazon.nova-pro-v1:0/converse-stream",
            "application/vnd.amazon.eventstream",
        )
        #
        assert bound._framer is not None  # pylint: disable=W0212

    def test_get_stamps_the_model_from_the_path(self, registered_dialects):
        bound = registry.get(
            "bedrock.converse", "/model/eu.amazon.nova-pro-v1:0/converse", "application/json",
        )
        #
        assert bound.result().model_name == "eu.amazon.nova-pro-v1:0"


class TestExtensibility:
    """Acceptance criterion: a new dialect needs no edit under usage/sources/."""

    def test_a_test_local_dialect_registers_and_wins(self):
        class VendorXDialect:
            """Defined entirely inside this test file — nothing in the library knows it."""

            id = "vendor_x.chat"
            provider = "vendor_x"

            def __init__(self):
                self.reading = base.UsageReading(dialect=self.id)

            # A predicate on the class: the registry probes candidates without building them.
            @classmethod
            def matches(cls, endpoint, content_type, head):
                return "/vendor-x/" in (endpoint or "")

            def bind(self, endpoint, content_type):
                pass

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


class TestEmptyRegistry:
    """An importer that forgets register_defaults() must not read as 'unknown endpoint'."""

    def test_match_on_an_empty_registry_returns_none_and_warns(self, monkeypatch):
        registry.clear()
        calls = []
        monkeypatch.setattr(registry.log, "warning", lambda msg, *a, **k: calls.append(msg % a))
        try:
            assert registry.match("/v1/chat/completions", "application/json") is None
            assert any("registry is empty" in call for call in calls)
        finally:
            registry.register_defaults()


class TestNarrowingLeavesATrail:
    """Falling back from a narrowed set to sniffing must be visible in the log."""

    def test_a_fallback_to_sniffing_logs_debug(self, registered_dialects, monkeypatch):
        calls = []
        monkeypatch.setattr(registry.log, "debug", lambda msg, *a, **k: calls.append(msg % a))
        #
        matched = registry.match("/v1/embeddings", "application/json", provider="ai_dial")
        #
        assert matched.id == "openai.embeddings"
        assert any("sniffing all" in call for call in calls)

    def test_a_provider_that_matches_logs_nothing(self, registered_dialects, monkeypatch):
        calls = []
        monkeypatch.setattr(registry.log, "debug", lambda msg, *a, **k: calls.append(msg % a))
        #
        registry.match("/v1/chat/completions", "application/json", provider="ai_dial")
        #
        assert calls == []


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

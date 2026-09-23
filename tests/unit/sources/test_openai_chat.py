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

""" openai.chat — the shape most of the fleet speaks.

Cached tokens here are *part of* prompt_tokens, so billable input subtracts them. Getting that
backwards is the single most expensive mistake this library can make, which is why the
convention is asserted directly and not only through the corpus sweep.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read, sse_body

CHUNK_SIZES = [0, 1, 7, 64, 4096]

BODY = {
    "id": "chatcmpl-abc", "object": "chat.completion", "model": "gpt-4o",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    "usage": {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 40},
        "completion_tokens_details": {"reasoning_tokens": 8},
    },
}

DELTA = {
    "id": "chatcmpl-abc", "object": "chat.completion.chunk", "model": "gpt-4o",
    "choices": [{"index": 0, "delta": {"content": "hi"}}],
}

FINAL = {
    "id": "chatcmpl-abc", "object": "chat.completion.chunk", "model": "gpt-4o",
    "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20},
}


class TestDispatch:
    """matches() truth table, including the head-bytes fallback."""

    @pytest.mark.parametrize("endpoint", [
        "/v1/chat/completions",
        "/chat/completions",
        "/v1/completions",
        "/llm/v1/chat/completions",
    ])
    def test_owns_the_openai_chat_paths(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json").id == "openai.chat"

    def test_claims_an_unknown_path_by_body_shape(self, registered_dialects):
        # Stage 2: a proxy that rewrote the path still reports prompt_tokens, and no other
        # dialect uses that key.
        matched = registry.match("/opaque/passthrough", "application/json",
                                 b'{"usage": {"prompt_tokens": 3}}')
        #
        assert matched.id == "openai.chat"

    @pytest.mark.parametrize("endpoint,owner", [
        ("/v1/messages", "anthropic.messages"),
        ("/v1/embeddings", "openai.embeddings"),
        ("/v1/responses", "openai.responses"),
        ("/api/chat", "ollama.native"),
    ])
    def test_leaves_the_neighbours_alone(self, registered_dialects, endpoint, owner):
        assert registry.match(endpoint, "application/json").id == owner


class TestNonStreaming:
    def test_reads_every_field(self, registered_dialects):
        reading = read("openai.chat", "/v1/chat/completions", "application/json",
                       json_body(BODY))
        #
        assert reading.model_name == "gpt-4o"
        assert (reading.input_tokens, reading.output_tokens) == (100, 20)
        assert reading.cache_read_tokens == 40
        assert reading.reasoning_tokens == 8

    def test_cached_tokens_are_subtracted(self, registered_dialects):
        # Inclusive convention: 40 of the 100 prompt tokens were served from cache.
        reading = read("openai.chat", "/v1/chat/completions", "application/json",
                       json_body(BODY))
        #
        assert reading.cache_convention == "inclusive"
        assert billable_input_tokens(reading) == 60


class TestStreaming:
    def test_usage_arrives_in_the_final_chunk(self, registered_dialects):
        body = sse_body([DELTA, DELTA, FINAL])
        #
        reading = read("openai.chat", "/v1/chat/completions", "text/event-stream", body)
        #
        assert (reading.input_tokens, reading.output_tokens) == (100, 20)

    def test_a_stream_without_usage_reports_nothing_rather_than_zero(self, registered_dialects):
        # Without stream_options.include_usage the usage chunk never comes. None here is what
        # lets the drainer fall back to an estimate instead of billing a free call.
        body = sse_body([DELTA, DELTA])
        #
        reading = read("openai.chat", "/v1/chat/completions", "text/event-stream", body)
        #
        assert reading.input_tokens is None and reading.output_tokens is None
        assert billable_input_tokens(reading) is None

    def test_the_word_usage_in_content_is_not_a_key(self, registered_dialects):
        # A model talking about its own token usage must not be mistaken for a usage block.
        chatter = dict(DELTA)
        chatter["choices"] = [{"index": 0, "delta": {"content": '"usage" {"prompt_tokens": 9}'}}]
        #
        reading = read("openai.chat", "/v1/chat/completions", "text/event-stream",
                       sse_body([chatter]))
        #
        assert reading.input_tokens is None

    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
    def test_chunking_changes_nothing(self, registered_dialects, chunk_size):
        body = sse_body([DELTA, FINAL])
        #
        reading = read("openai.chat", "/v1/chat/completions", "text/event-stream",
                       body, chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (100, 20)

    def test_truncation_never_raises(self, registered_dialects):
        body = sse_body([DELTA, FINAL])
        #
        for cut in range(0, len(body), 13):
            read("openai.chat", "/v1/chat/completions", "text/event-stream", body[:cut])


class TestLiteLLMBedrockNormalisation:
    """LiteLLM reshapes Bedrock/Anthropic replies to this dialect and emits *both* cache
    conventions in one body: nested OpenAI-style `prompt_tokens_details` and top-level
    Anthropic-style `cache_read_input_tokens`. Only the nested pair may be read, or the same
    cached tokens get subtracted twice. Captured from a real relayed call.
    """

    CACHE_HIT = {
        "usage": {
            "completion_tokens": 4, "prompt_tokens": 2731, "total_tokens": 2735,
            "completion_tokens_details": {"reasoning_tokens": 0, "text_tokens": 4},
            "prompt_tokens_details": {
                "cached_tokens": 2719, "text_tokens": 12, "cache_creation_tokens": 0,
            },
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 2719,
        },
    }
    CACHE_WRITE = {
        "usage": {
            "completion_tokens": 4, "prompt_tokens": 2731, "total_tokens": 2735,
            "prompt_tokens_details": {
                "cached_tokens": 0, "text_tokens": 12, "cache_creation_tokens": 2719,
            },
            "cache_creation_input_tokens": 2719, "cache_read_input_tokens": 0,
        },
    }

    def test_a_cache_read_is_subtracted_exactly_once(self, registered_dialects):
        reading = read("openai.chat", "/v1/chat/completions", "application/json",
                       json_body(self.CACHE_HIT))
        #
        assert reading.cache_read_tokens == 2719
        assert billable_input_tokens(reading) == 12

    def test_a_cache_write_is_subtracted_exactly_once(self, registered_dialects):
        # prompt_tokens (2731) = text_tokens (12) + cache_creation_tokens (2719). The writes
        # are priced separately at the cache-write rate, so leaving them in billable input
        # charged them twice and made total_tokens ~2x LiteLLM's (issue: nested-agent
        # Analytics totals diverging from LiteLLM on Claude-over-chat/completions).
        reading = read("openai.chat", "/v1/chat/completions", "application/json",
                       json_body(self.CACHE_WRITE))
        #
        assert reading.cache_creation_tokens == 2719
        assert billable_input_tokens(reading) == 12

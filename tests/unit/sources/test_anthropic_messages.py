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

""" anthropic.messages — two traps, both of which have cost real money elsewhere.

**The placeholder.** `message_start` reports `output_tokens: 1`. It is not a partial count, it is
a placeholder, and only `message_delta` carries the real figure. So output is *latest wins* while
input and cache counts are *first wins* — `message_delta` re-reports input as 0 in some versions,
and letting that overwrite a real prompt count would zero out the expensive half of the bill.

**The convention.** Cached tokens here are *additional to* `input_tokens`, not part of it. The
same numbers under OpenAI's inclusive convention would bill differently, so nothing may be
subtracted.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read, sse_body

BODY = {
    "id": "msg_1", "type": "message", "role": "assistant",
    "model": "claude-sonnet-4-5-20250929",
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 90, "output_tokens": 24,
              "cache_read_input_tokens": 30, "cache_creation_input_tokens": 12},
}

MESSAGE_START = {
    "type": "message_start",
    "message": {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-5-20250929", "content": [],
        # output_tokens: 1 is Anthropic's placeholder, not a count.
        "usage": {"input_tokens": 10, "output_tokens": 1,
                  "cache_read_input_tokens": 4, "cache_creation_input_tokens": 0},
    },
}

CONTENT_DELTA = {
    "type": "content_block_delta", "index": 0,
    "delta": {"type": "text_delta", "text": "hello there"},
}

MESSAGE_DELTA = {
    "type": "message_delta",
    "delta": {"stop_reason": "end_turn"},
    # Anthropic re-reports input as 0 here; first-wins is what protects the real count.
    "usage": {"input_tokens": 0, "output_tokens": 2},
}


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [
        "/v1/messages",
        "/messages",
        "/v1/messages/batches",
    ])
    def test_owns_the_messages_paths(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json").id == "anthropic.messages"

    @pytest.mark.parametrize("head", [
        b'{"usage": {"input_tokens": 5, "cache_read_input_tokens": 1}}',
        b'{"type": "message_start", "message": {"usage": {"input_tokens": 5}}}',
    ])
    def test_claims_an_unknown_path_by_signature(self, registered_dialects, head):
        assert registry.match("/opaque/passthrough", "text/event-stream", head).id == \
            "anthropic.messages"

    def test_does_not_claim_the_responses_endpoint(self, registered_dialects):
        # Same key names, opposite convention — this boundary is the expensive one.
        assert registry.match("/v1/responses", "application/json").id == "openai.responses"


class TestNonStreaming:
    def test_reads_both_cache_counters(self, registered_dialects):
        reading = read("anthropic.messages", "/v1/messages", "application/json",
                       json_body(BODY))
        #
        assert reading.model_name == "claude-sonnet-4-5-20250929"
        assert (reading.input_tokens, reading.output_tokens) == (90, 24)
        assert reading.cache_read_tokens == 30
        assert reading.cache_creation_tokens == 12

    def test_cached_tokens_are_never_subtracted(self, registered_dialects):
        reading = read("anthropic.messages", "/v1/messages", "application/json",
                       json_body(BODY))
        #
        assert reading.cache_convention == "exclusive"
        assert billable_input_tokens(reading) == 90


class TestStreamingMergeRules:
    def stream(self, chunk_size=0):
        body = sse_body([MESSAGE_START, CONTENT_DELTA, CONTENT_DELTA, MESSAGE_DELTA])
        return read("anthropic.messages", "/v1/messages", "text/event-stream",
                    body, chunk_size)

    def test_message_delta_overwrites_the_placeholder(self, registered_dialects):
        assert self.stream().output_tokens == 2

    def test_input_keeps_the_first_observation(self, registered_dialects):
        # message_delta reported 0; the real prompt count came from message_start.
        assert self.stream().input_tokens == 10

    def test_cache_counts_keep_the_first_observation(self, registered_dialects):
        reading = self.stream()
        #
        assert reading.cache_read_tokens == 4
        assert reading.cache_creation_tokens == 0

    def test_a_stream_cut_before_message_delta_keeps_the_placeholder(self, registered_dialects):
        # Documented consequence, not a defect: without message_delta the only output figure
        # ever sent is the placeholder. #6571 decides whether to prefer an estimate here.
        body = sse_body([MESSAGE_START, CONTENT_DELTA], done=False)
        #
        reading = read("anthropic.messages", "/v1/messages", "text/event-stream", body)
        #
        assert reading.output_tokens == 1
        assert reading.input_tokens == 10

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_chunking_changes_nothing(self, registered_dialects, chunk_size):
        reading = self.stream(chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (10, 2)
        assert reading.cache_read_tokens == 4

    def test_truncation_never_raises(self, registered_dialects):
        body = sse_body([MESSAGE_START, CONTENT_DELTA, MESSAGE_DELTA])
        #
        for cut in range(0, len(body), 13):
            read("anthropic.messages", "/v1/messages", "text/event-stream", body[:cut])

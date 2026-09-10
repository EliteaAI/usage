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

""" bedrock.invoke — Bedrock's own metrics over whatever body the underlying model speaks.

InvokeModel is a passthrough: the body is the model vendor's, so an Anthropic model returns
Anthropic keys and a Nova model returns camelCase ones. Bedrock adds its own
`amazon-bedrock-invocationMetrics` block, and on a streamed call each chunk arrives as a binary
frame whose payload wraps the model's JSON in a base64 `bytes` field — two envelopes deep.

Both key families are accepted, and both are exclusive-cache, so a mixed body cannot produce a
convention mismatch.
"""

import base64
import json

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read

MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"
ENDPOINT = f"/model/{MODEL}/invoke"
STREAM_ENDPOINT = f"/model/{MODEL}/invoke-with-response-stream"
EVENT_STREAM_TYPE = "application/vnd.amazon.eventstream"

ANTHROPIC_BODY = {
    "id": "msg_bd", "type": "message", "role": "assistant",
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 70, "output_tokens": 41, "cache_read_input_tokens": 12},
}

METRICS = {
    "amazon-bedrock-invocationMetrics": {
        "inputTokenCount": 70, "outputTokenCount": 41,
        "invocationLatency": 900, "firstByteLatency": 300,
    },
}


@pytest.fixture()
def builder():
    pytest.importorskip("botocore")
    return pytest.importorskip("eventstream_builder")


def wrapped(builder, payload):
    """One streamed chunk: a binary frame whose payload base64-wraps the model's body."""
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return builder.encode_event("chunk", {"bytes": encoded})


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [ENDPOINT, STREAM_ENDPOINT])
    def test_owns_both_invoke_paths(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json").id == "bedrock.invoke"

    def test_does_not_claim_converse(self, registered_dialects):
        assert registry.match(f"/model/{MODEL}/converse", "application/json").id == \
            "bedrock.converse"

    def test_the_model_comes_from_the_path(self, registered_dialects):
        reading = read("bedrock.invoke", ENDPOINT, "application/json",
                       json_body(ANTHROPIC_BODY))
        #
        assert reading.model_name == MODEL


class TestPassthroughBodies:
    def test_reads_the_vendor_body_when_metrics_are_absent(self, registered_dialects):
        # Non-streamed InvokeModel returns only the model's own body.
        reading = read("bedrock.invoke", ENDPOINT, "application/json",
                       json_body(ANTHROPIC_BODY))
        #
        assert (reading.input_tokens, reading.output_tokens) == (70, 41)
        assert reading.cache_read_tokens == 12

    def test_snake_case_and_camel_case_agree_on_the_convention(self, registered_dialects):
        nova = {"usage": {"inputTokens": 70, "outputTokens": 41}}
        #
        reading = read("bedrock.invoke", ENDPOINT, "application/json", json_body(nova))
        #
        assert (reading.input_tokens, reading.output_tokens) == (70, 41)
        assert reading.cache_convention == "exclusive"

    def test_cached_tokens_are_never_subtracted(self, registered_dialects):
        reading = read("bedrock.invoke", ENDPOINT, "application/json",
                       json_body(ANTHROPIC_BODY))
        #
        assert billable_input_tokens(reading) == 70

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_chunking_changes_nothing(self, registered_dialects, chunk_size):
        reading = read("bedrock.invoke", ENDPOINT, "application/json",
                       json_body(ANTHROPIC_BODY), chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (70, 41)


class TestBinaryStreaming:
    def stream(self, builder):
        return b"".join([
            wrapped(builder, {"type": "message_start", "message": {
                "usage": {"input_tokens": 70, "output_tokens": 1,
                          "cache_read_input_tokens": 12},
            }}),
            wrapped(builder, {"type": "content_block_delta",
                              "delta": {"type": "text_delta", "text": "hi"}}),
            wrapped(builder, {"type": "message_delta",
                              "usage": {"output_tokens": 41}, **METRICS}),
        ])

    def test_reads_through_both_envelopes(self, registered_dialects, builder):
        reading = read("bedrock.invoke", STREAM_ENDPOINT, EVENT_STREAM_TYPE,
                       self.stream(builder))
        #
        assert (reading.input_tokens, reading.output_tokens) == (70, 41)
        assert reading.cache_read_tokens == 12

    @pytest.mark.parametrize("chunk_size", [1, 7, 64, 4096])
    def test_a_frame_split_across_feeds_is_reassembled(
        self, registered_dialects, builder, chunk_size,
    ):
        reading = read("bedrock.invoke", STREAM_ENDPOINT, EVENT_STREAM_TYPE,
                       self.stream(builder), chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (70, 41)

    def test_bedrocks_own_metrics_are_read(self, registered_dialects, builder):
        # Metrics alone, no vendor usage block: this is the shape a non-Anthropic model gives.
        body = wrapped(builder, dict(METRICS))
        #
        reading = read("bedrock.invoke", STREAM_ENDPOINT, EVENT_STREAM_TYPE, body)
        #
        assert (reading.input_tokens, reading.output_tokens) == (70, 41)

    def test_truncation_never_raises(self, registered_dialects, builder):
        body = self.stream(builder)
        #
        for cut in range(0, len(body), 9):
            read("bedrock.invoke", STREAM_ENDPOINT, EVENT_STREAM_TYPE, body[:cut])

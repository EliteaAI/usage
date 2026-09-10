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

""" bedrock.converse — camelCase counts, a binary envelope, and no model name in the body.

Three things set this dialect apart. The counts are camelCase (`inputTokens`, not
`input_tokens`). Streaming arrives as AWS binary event-stream frames rather than SSE, so the
framing has to be decoded before any key is visible. And the body never names the model — the
invoked model id lives in the request path, which is why `matches()` reads it from there.

Cache tokens follow Anthropic's exclusive convention, since the models behind Converse are
largely Anthropic's.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read

MODEL = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
NON_STREAM_ENDPOINT = f"/model/{MODEL}/converse"
STREAM_ENDPOINT = f"/model/{MODEL}/converse-stream"
EVENT_STREAM_TYPE = "application/vnd.amazon.eventstream"

BODY = {
    "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 120, "outputTokens": 34, "totalTokens": 154,
              "cacheReadInputTokens": 45, "cacheWriteInputTokens": 20},
    "metrics": {"latencyMs": 812},
}


def frames(builder, events):
    """Encode a list of (event type, payload) pairs as one binary event-stream."""
    return b"".join(builder.encode_event(name, payload) for name, payload in events)


@pytest.fixture()
def builder():
    pytest.importorskip("botocore")
    return pytest.importorskip("eventstream_builder")


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [NON_STREAM_ENDPOINT, STREAM_ENDPOINT])
    def test_owns_both_converse_paths(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json").id == "bedrock.converse"

    def test_does_not_claim_invoke(self, registered_dialects):
        assert registry.match(f"/model/{MODEL}/invoke", "application/json").id == \
            "bedrock.invoke"

    def test_an_anthropic_model_id_in_the_path_is_not_the_messages_dialect(
        self, registered_dialects,
    ):
        # The path names an Anthropic model, but the wire dialect is Bedrock's, not Anthropic's.
        assert registry.match(NON_STREAM_ENDPOINT, "application/json").id == "bedrock.converse"


class TestNonStreaming:
    def test_reads_the_camel_case_counts(self, registered_dialects):
        reading = read("bedrock.converse", NON_STREAM_ENDPOINT, "application/json",
                       json_body(BODY))
        #
        assert (reading.input_tokens, reading.output_tokens) == (120, 34)
        assert reading.cache_read_tokens == 45
        assert reading.cache_creation_tokens == 20

    def test_the_model_comes_from_the_path(self, registered_dialects):
        reading = read("bedrock.converse", NON_STREAM_ENDPOINT, "application/json",
                       json_body(BODY))
        #
        assert reading.model_name == MODEL

    def test_cached_tokens_are_never_subtracted(self, registered_dialects):
        reading = read("bedrock.converse", NON_STREAM_ENDPOINT, "application/json",
                       json_body(BODY))
        #
        assert reading.cache_convention == "exclusive"
        assert billable_input_tokens(reading) == 120

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_chunking_changes_nothing(self, registered_dialects, chunk_size):
        reading = read("bedrock.converse", NON_STREAM_ENDPOINT, "application/json",
                       json_body(BODY), chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (120, 34)


class TestBinaryStreaming:
    def stream(self, builder):
        return frames(builder, [
            ("messageStart", {"role": "assistant"}),
            ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "hi"}}),
            ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": " there"}}),
            ("messageStop", {"stopReason": "end_turn"}),
            ("metadata", {"usage": {"inputTokens": 120, "outputTokens": 34,
                                    "cacheReadInputTokens": 45},
                          "metrics": {"latencyMs": 900}}),
        ])

    def test_the_metadata_event_arrives_last_and_still_lands(
        self, registered_dialects, builder,
    ):
        reading = read("bedrock.converse", STREAM_ENDPOINT, EVENT_STREAM_TYPE,
                       self.stream(builder))
        #
        assert (reading.input_tokens, reading.output_tokens) == (120, 34)
        assert reading.cache_read_tokens == 45

    @pytest.mark.parametrize("chunk_size", [1, 7, 64, 4096])
    def test_a_frame_split_across_feeds_is_reassembled(
        self, registered_dialects, builder, chunk_size,
    ):
        # Frame boundaries have nothing to do with TCP boundaries, so every split has to work.
        reading = read("bedrock.converse", STREAM_ENDPOINT, EVENT_STREAM_TYPE,
                       self.stream(builder), chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (120, 34)

    def test_truncation_never_raises(self, registered_dialects, builder):
        body = self.stream(builder)
        #
        for cut in range(0, len(body), 9):
            read("bedrock.converse", STREAM_ENDPOINT, EVENT_STREAM_TYPE, body[:cut])

    def test_a_plain_json_body_on_the_stream_path_is_still_read(
        self, registered_dialects,
    ):
        # An error or a non-streamed retry can come back as JSON on the same endpoint; the
        # content-type, not the path, is what selects the framer.
        reading = read("bedrock.converse", STREAM_ENDPOINT, "application/json",
                       json_body(BODY))
        #
        assert reading.input_tokens == 120

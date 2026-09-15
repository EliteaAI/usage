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

""" openai.responses — the same key names as Anthropic, the opposite cache convention.

`input_tokens` / `output_tokens` / cached tokens appear in both dialects. Here cached tokens are
part of the input; in Anthropic they are additional to it. Nothing in the numbers themselves says
which is which, so dispatch has to be right or the bill is wrong. The `*_tokens_details`
sub-objects are the only body-level tell, and the endpoint is the primary one.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read, sse_body

BODY = {
    "id": "resp_1", "object": "response", "model": "gpt-5-mini", "status": "completed",
    "output": [{"type": "message", "role": "assistant", "content": []}],
    "usage": {
        "input_tokens": 200, "output_tokens": 55, "total_tokens": 255,
        "input_tokens_details": {"cached_tokens": 128},
        "output_tokens_details": {"reasoning_tokens": 30},
    },
}

COMPLETED_EVENT = {"type": "response.completed", "response": BODY}


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [
        "/v1/responses",
        "/responses",
        "/v1/responses/resp_1",
    ])
    def test_owns_the_responses_paths(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json").id == "openai.responses"

    def test_claims_an_unknown_path_by_the_details_sub_objects(self, registered_dialects):
        matched = registry.match("/opaque", "application/json",
                                 b'{"usage": {"input_tokens": 5, '
                                 b'"input_tokens_details": {"cached_tokens": 0}}}')
        #
        assert matched.id == "openai.responses"

    def test_bare_input_tokens_without_details_is_left_to_anthropic(self, registered_dialects):
        # This is the ambiguous case, and it must not be resolved by guessing: the Anthropic
        # signature key is what decides it.
        matched = registry.match("/opaque", "application/json",
                                 b'{"usage": {"input_tokens": 5, '
                                 b'"cache_read_input_tokens": 2}}')
        #
        assert matched.id == "anthropic.messages"

    def test_does_not_claim_the_anthropic_endpoint(self, registered_dialects):
        assert registry.match("/v1/messages", "application/json").id == "anthropic.messages"


class TestReading:
    def test_reads_the_details_sub_objects(self, registered_dialects):
        reading = read("openai.responses", "/v1/responses", "application/json",
                       json_body(BODY))
        #
        assert reading.model_name == "gpt-5-mini"
        assert (reading.input_tokens, reading.output_tokens) == (200, 55)
        assert reading.cache_read_tokens == 128
        assert reading.reasoning_tokens == 30

    def test_cached_input_is_subtracted_unlike_anthropic(self, registered_dialects):
        reading = read("openai.responses", "/v1/responses", "application/json",
                       json_body(BODY))
        #
        assert reading.cache_convention == "inclusive"
        assert billable_input_tokens(reading) == 72

    def test_reasoning_is_already_inside_output(self, registered_dialects):
        # Unlike Gemini, OpenAI counts reasoning tokens within output_tokens, so output must be
        # taken as reported rather than summed.
        reading = read("openai.responses", "/v1/responses", "application/json",
                       json_body(BODY))
        #
        assert reading.output_tokens == 55

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_streaming_is_chunk_independent(self, registered_dialects, chunk_size):
        body = sse_body([
            {"type": "response.output_text.delta", "delta": "hi"},
            COMPLETED_EVENT,
        ])
        #
        reading = read("openai.responses", "/v1/responses", "text/event-stream",
                       body, chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (200, 55)
        assert reading.cache_read_tokens == 128

    def test_truncation_never_raises(self, registered_dialects):
        body = sse_body([COMPLETED_EVENT])
        #
        for cut in range(0, len(body), 17):
            read("openai.responses", "/v1/responses", "text/event-stream", body[:cut])


class TestImageGeneration:
    """Image responses carry this exact token shape, but `usage` sits after the base64 payload.

    Head sniffing can therefore never reach the `*_tokens_details` tell, so the endpoint has to
    be the thing that claims the body — otherwise every image generation bills as zero.
    """

    IMAGE_BODY = {
        "created": 1, "background": "opaque", "size": "1024x1024",
        "data": [{"b64_json": "A" * 4096}],
        "usage": {
            "total_tokens": 206, "input_tokens": 10, "output_tokens": 196,
            "input_tokens_details": {"image_tokens": 0, "text_tokens": 10},
            "output_tokens_details": {"image_tokens": 196, "text_tokens": 0},
        },
    }

    @pytest.mark.parametrize("endpoint", [
        "/v1/images/generations", "/v1/images/edits", "/v1/images/variations",
    ])
    def test_the_endpoint_claims_the_body(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json", b"").id == "openai.responses"

    def test_image_tokens_are_read(self, registered_dialects):
        reading = read("openai.responses", "/v1/images/generations", "application/json",
                       json_body(self.IMAGE_BODY))
        #
        assert (reading.input_tokens, reading.output_tokens) == (10, 196)

    def test_a_leading_payload_does_not_hide_the_usage_from_sniffing(self, registered_dialects):
        # The tell the head probe would have used is genuinely absent from the first bytes
        head = json_body(self.IMAGE_BODY)[:512]
        #
        assert b'"input_tokens_details"' not in head
        assert registry.match("/v1/images/generations", "application/json", head) is not None

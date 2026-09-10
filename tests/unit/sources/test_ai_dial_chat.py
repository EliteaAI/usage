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

""" ai_dial.chat — EPAM DIAL, which is OpenAI-shaped by design.

The fixture for this dialect is openly derived from the openai.chat one: DIAL proxies to the
same upstreams and returns the same body, so a separate hand-built payload would be fiction
dressed up as evidence. What is genuinely DIAL-specific is the addressing, and that is what
gets the truth table.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read, sse_body

DIAL_ENDPOINT = "/openai/deployments/gpt-4o/chat/completions"

BODY = {
    "id": "chatcmpl-dial", "object": "chat.completion", "model": "gpt-4o",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 51, "completion_tokens": 12, "total_tokens": 63,
              "prompt_tokens_details": {"cached_tokens": 11}},
}


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [
        DIAL_ENDPOINT,
        "/openai/deployments/anthropic.claude-v3/chat/completions",
        "/dial/openai/deployments/gpt-4o/chat/completions",
        "/v1/dial/chat/completions",
    ])
    def test_owns_dial_addressing(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json").id == "ai_dial.chat"

    def test_an_api_version_hands_even_a_named_dial_path_to_azure(self, registered_dialects):
        # Registration order puts Azure first, and `api-version` on a deployment path is its
        # signature. DIAL fronting an Azure upstream this way is labelled azure.chat — the
        # numbers and the convention are identical, so only the label differs.
        matched = registry.match("/dial/openai/deployments/gpt-4o/chat/completions"
                                 "?api-version=2024-06-01", "application/json")
        #
        assert matched.id == "azure.chat"

    def test_yields_a_bare_deployment_path_to_azure_when_versioned(self, registered_dialects):
        matched = registry.match(f"{DIAL_ENDPOINT}?api-version=2024-06-01", "application/json")
        #
        assert matched.id == "azure.chat"

    def test_does_not_claim_a_plain_openai_path(self, registered_dialects):
        assert registry.match("/v1/chat/completions", "application/json").id == "openai.chat"


class TestReading:
    def test_reads_the_openai_shape(self, registered_dialects):
        reading = read("ai_dial.chat", DIAL_ENDPOINT, "application/json", json_body(BODY))
        #
        assert reading.model_name == "gpt-4o"
        assert (reading.input_tokens, reading.output_tokens) == (51, 12)
        assert reading.cache_read_tokens == 11

    def test_inherits_the_inclusive_convention(self, registered_dialects):
        reading = read("ai_dial.chat", DIAL_ENDPOINT, "application/json", json_body(BODY))
        #
        assert reading.cache_convention == "inclusive"
        assert billable_input_tokens(reading) == 40

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_streaming_is_chunk_independent(self, registered_dialects, chunk_size):
        body = sse_body([
            {"model": "gpt-4o", "choices": [{"delta": {"content": "ok"}}]},
            {"model": "gpt-4o", "choices": [],
             "usage": {"prompt_tokens": 51, "completion_tokens": 12}},
        ])
        #
        reading = read("ai_dial.chat", DIAL_ENDPOINT, "text/event-stream", body, chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (51, 12)

    def test_truncation_never_raises(self, registered_dialects):
        body = json_body(BODY)
        #
        for cut in range(0, len(body), 11):
            read("ai_dial.chat", DIAL_ENDPOINT, "application/json", body[:cut])

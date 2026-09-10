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

""" azure.chat — OpenAI's body, Azure's addressing.

Azure adds nothing to the payload, so this dialect exists purely to label the reading. What it
must get right is the *boundary*: a deployment path with `api-version` is Azure, the same path
without it is DIAL, and the platform's own normalized proxy path is neither.

That last case is deliberate and worth stating plainly: because the proxy rewrites everything to
`/v1/chat/completions`, this dialect only fires on engine-side native calls. A response labelled
`openai.chat` instead of `azure.chat` carries identical numbers under an identical convention, so
the mislabel is cosmetic — but the label is what tells us which layer observed the call.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read, sse_body

AZURE_ENDPOINT = "/openai/deployments/gpt-4o/chat/completions?api-version=2024-06-01"

BODY = {
    "id": "chatcmpl-az", "object": "chat.completion", "model": "gpt-4o-2024-08-06",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {
        "prompt_tokens": 64, "completion_tokens": 9, "total_tokens": 73,
        "prompt_tokens_details": {"cached_tokens": 0},
    },
}


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [
        AZURE_ENDPOINT,
        "/openai/deployments/my-dep/chat/completions?api-version=2025-01-01-preview",
        "/openai/deployments/my-dep/completions?api-version=2024-06-01",
    ])
    def test_owns_a_deployment_path_carrying_an_api_version(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json").id == "azure.chat"

    def test_a_deployment_path_without_api_version_is_dial(self, registered_dialects):
        matched = registry.match("/openai/deployments/gpt-4o/chat/completions",
                                 "application/json")
        #
        assert matched.id == "ai_dial.chat"

    def test_the_api_version_alone_is_not_enough(self, registered_dialects):
        # No deployment segment: this is a plain OpenAI-compatible endpoint that happens to
        # take a version parameter.
        matched = registry.match("/v1/chat/completions?api-version=2024-06-01",
                                 "application/json")
        #
        assert matched.id == "openai.chat"

    def test_the_normalized_proxy_path_falls_through_to_openai_chat(self, registered_dialects):
        assert registry.match("/v1/chat/completions", "application/json").id == "openai.chat"


class TestReading:
    def test_parses_exactly_like_openai_chat(self, registered_dialects):
        reading = read("azure.chat", AZURE_ENDPOINT, "application/json", json_body(BODY))
        #
        assert reading.model_name == "gpt-4o-2024-08-06"
        assert (reading.input_tokens, reading.output_tokens) == (64, 9)
        assert reading.cache_convention == "inclusive"
        assert billable_input_tokens(reading) == 64

    def test_an_observed_zero_is_not_a_missing_value(self, registered_dialects):
        # cached_tokens: 0 was reported, and 0 is what we bill against.
        reading = read("azure.chat", AZURE_ENDPOINT, "application/json", json_body(BODY))
        #
        assert reading.cache_read_tokens == 0

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_streaming_is_chunk_independent(self, registered_dialects, chunk_size):
        body = sse_body([
            {"model": "gpt-4o-2024-08-06", "choices": [{"delta": {"content": "ok"}}]},
            {"model": "gpt-4o-2024-08-06", "choices": [],
             "usage": {"prompt_tokens": 64, "completion_tokens": 9}},
        ])
        #
        reading = read("azure.chat", AZURE_ENDPOINT, "text/event-stream", body, chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (64, 9)

    def test_truncation_never_raises(self, registered_dialects):
        body = json_body(BODY)
        #
        for cut in range(0, len(body), 11):
            read("azure.chat", AZURE_ENDPOINT, "application/json", body[:cut])

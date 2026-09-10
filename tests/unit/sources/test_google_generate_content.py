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

""" google.generate_content — the one dialect where reported output is not billable output.

`thoughtsTokenCount` is billed as output but sits **outside** `candidatesTokenCount`, so output is
their sum. Taking `candidatesTokenCount` at face value under-bills every thinking model, silently,
and by a large margin — litellm's own test is the oracle here: 1442 candidates + 158 thoughts is
1600 output tokens.

The second trap is cheaper but easy: `cachedContentTokenCount` is part of `promptTokenCount`
(inclusive), so it is subtracted exactly once in `billable_input_tokens` and nowhere else.

`usageMetadata` is also **cumulative per streamed chunk**, not incremental, so the last one wins
rather than being accumulated.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read, sse_body

ENDPOINT = "/v1beta/models/gemini-2.5-flash:generateContent"
STREAM_ENDPOINT = "/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"

# Counts from litellm's own thinking-model test: output is 1442 + 158, not 1442.
THINKING = {
    "modelVersion": "gemini-2.5-flash",
    "candidates": [{"content": {"parts": [{"text": "hi"}], "role": "model"},
                    "finishReason": "STOP"}],
    "usageMetadata": {
        "promptTokenCount": 20, "candidatesTokenCount": 1442,
        "thoughtsTokenCount": 158, "totalTokenCount": 1620,
    },
}

PLAIN = {
    "modelVersion": "gemini-2.0-flash",
    "candidates": [{"content": {"parts": [{"text": "hi"}], "role": "model"}}],
    "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 14,
                      "totalTokenCount": 23},
}

CACHED = {
    "modelVersion": "gemini-2.5-flash",
    "candidates": [{"content": {"parts": [{"text": "hi"}], "role": "model"}}],
    "usageMetadata": {"promptTokenCount": 5000, "candidatesTokenCount": 31,
                      "cachedContentTokenCount": 4096, "totalTokenCount": 5031},
}


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [
        ENDPOINT,
        STREAM_ENDPOINT,
        "/v1/projects/p/locations/eu/publishers/google/models/gemini-2.5-pro:generateContent",
    ])
    def test_owns_both_generate_content_verbs(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/json").id == "google.generate_content"

    def test_claims_an_unknown_path_by_usage_metadata(self, registered_dialects):
        matched = registry.match("/opaque", "application/json",
                                 b'{"usageMetadata": {"promptTokenCount": 1}}')
        #
        assert matched.id == "google.generate_content"

    def test_the_model_comes_from_model_version(self, registered_dialects):
        reading = read("google.generate_content", ENDPOINT, "application/json",
                       json_body(PLAIN))
        #
        assert reading.model_name == "gemini-2.0-flash"


class TestThinkingTokens:
    def test_output_is_candidates_plus_thoughts(self, registered_dialects):
        reading = read("google.generate_content", ENDPOINT, "application/json",
                       json_body(THINKING))
        #
        assert reading.output_tokens == 1600

    def test_thoughts_are_also_reported_separately(self, registered_dialects):
        # Billed as output, but kept visible so the cost split stays explainable.
        reading = read("google.generate_content", ENDPOINT, "application/json",
                       json_body(THINKING))
        #
        assert reading.reasoning_tokens == 158

    def test_a_non_thinking_response_is_untouched(self, registered_dialects):
        reading = read("google.generate_content", ENDPOINT, "application/json",
                       json_body(PLAIN))
        #
        assert reading.output_tokens == 14
        assert reading.reasoning_tokens == 0


class TestCachedContent:
    def test_cached_content_is_subtracted_once(self, registered_dialects):
        reading = read("google.generate_content", ENDPOINT, "application/json",
                       json_body(CACHED))
        #
        assert reading.cache_convention == "inclusive"
        assert reading.input_tokens == 5000
        assert reading.cache_read_tokens == 4096
        assert billable_input_tokens(reading) == 904

    def test_input_tokens_still_carries_the_full_prompt_count(self, registered_dialects):
        # The subtraction happens in billable_input_tokens and must not be baked into the
        # reading, or a second consumer would subtract it again.
        reading = read("google.generate_content", ENDPOINT, "application/json",
                       json_body(CACHED))
        #
        assert reading.input_tokens == 5000


class TestStreaming:
    def stream(self, chunk_size=0):
        # usageMetadata is cumulative per chunk: the last one is the whole truth.
        body = sse_body([
            {"modelVersion": "gemini-2.5-flash",
             "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
             "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 700,
                               "thoughtsTokenCount": 158}},
            {"modelVersion": "gemini-2.5-flash",
             "candidates": [{"content": {"parts": [{"text": " there"}]}}],
             "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 1442,
                               "thoughtsTokenCount": 158}},
        ], done=False)
        #
        return read("google.generate_content", STREAM_ENDPOINT, "text/event-stream",
                    body, chunk_size)

    def test_the_last_cumulative_report_wins(self, registered_dialects):
        assert self.stream().output_tokens == 1600

    def test_prompt_count_is_not_accumulated_across_chunks(self, registered_dialects):
        # Repeated verbatim in every chunk; summing it would multiply the input bill by the
        # number of chunks.
        assert self.stream().input_tokens == 20

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_chunking_changes_nothing(self, registered_dialects, chunk_size):
        reading = self.stream(chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (20, 1600)

    def test_truncation_never_raises(self, registered_dialects):
        body = json_body(THINKING)
        #
        for cut in range(0, len(body), 11):
            read("google.generate_content", ENDPOINT, "application/json", body[:cut])

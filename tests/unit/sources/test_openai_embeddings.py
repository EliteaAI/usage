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

""" openai.embeddings — input only, and that is the whole point.

An embeddings call has no completion, so `output_tokens` is pinned to an observed 0 rather than
left as None. That distinction matters downstream: None would make the drainer reach for a token
estimate that does not exist, while 0 is the truthful answer.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, read

BODY = {
    "object": "list", "model": "text-embedding-3-small",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
    "usage": {"prompt_tokens": 17, "total_tokens": 17},
}

TOTAL_ONLY = {
    "object": "list", "model": "text-embedding-3-large",
    "data": [], "usage": {"total_tokens": 42},
}


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [
        "/v1/embeddings",
        "/embeddings",
        "/openai/deployments/embed-3/embeddings?api-version=2024-06-01",
    ])
    def test_owns_every_embeddings_path(self, registered_dialects, endpoint):
        # The Azure-addressed variant lands here too: the embeddings path is more specific than
        # Azure's chat signature, and both read the same keys anyway.
        assert registry.match(endpoint, "application/json").id == "openai.embeddings"

    def test_does_not_claim_chat(self, registered_dialects):
        assert registry.match("/v1/chat/completions", "application/json").id == "openai.chat"


class TestReading:
    def test_output_is_an_observed_zero(self, registered_dialects):
        reading = read("openai.embeddings", "/v1/embeddings", "application/json",
                       json_body(BODY))
        #
        assert reading.input_tokens == 17
        assert reading.output_tokens == 0

    def test_falls_back_to_total_tokens(self, registered_dialects):
        # Some compatible servers report only a total. With no completion, the total *is* the
        # prompt count.
        reading = read("openai.embeddings", "/v1/embeddings", "application/json",
                       json_body(TOTAL_ONLY))
        #
        assert reading.input_tokens == 42

    def test_billable_input_needs_no_cache_adjustment(self, registered_dialects):
        reading = read("openai.embeddings", "/v1/embeddings", "application/json",
                       json_body(BODY))
        #
        assert reading.cache_convention == "inclusive"
        assert reading.cache_read_tokens == 0
        assert billable_input_tokens(reading) == 17

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_chunking_changes_nothing(self, registered_dialects, chunk_size):
        reading = read("openai.embeddings", "/v1/embeddings", "application/json",
                       json_body(BODY), chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (17, 0)

    def test_truncation_never_raises(self, registered_dialects):
        body = json_body(BODY)
        #
        for cut in range(0, len(body), 7):
            read("openai.embeddings", "/v1/embeddings", "application/json", body[:cut])

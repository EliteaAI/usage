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

""" ollama.native — flat top-level counters, NDJSON, no cache concept.

Ollama has no usage object: the counts are top-level scalars, and they appear only on the final
NDJSON line (`"done": true`). Earlier lines carry none, so a stream that dies early legitimately
reports nothing at all.

There is no prompt caching here, so both cache counters stay at zero and the convention is
irrelevant — exclusive is chosen so nothing is ever subtracted from a count that has no cached
part.
"""

import pytest

from usage.sources import registry
from usage.sources.base import billable_input_tokens

from corpus import json_body, ndjson_body, read

DELTA = {"model": "llama3.2", "created_at": "2026-01-01T00:00:00Z",
         "message": {"role": "assistant", "content": "hi"}, "done": False}

FINAL = {"model": "llama3.2", "created_at": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "content": ""}, "done": True,
         "done_reason": "stop", "total_duration": 1200000,
         "prompt_eval_count": 26, "eval_count": 298}


class TestDispatch:
    @pytest.mark.parametrize("endpoint", [
        "/api/chat", "/api/generate", "/api/embed", "/api/embeddings",
    ])
    def test_owns_the_native_api_paths(self, registered_dialects, endpoint):
        assert registry.match(endpoint, "application/x-ndjson").id == "ollama.native"

    def test_claims_an_unknown_path_by_the_flat_counters(self, registered_dialects):
        matched = registry.match("/opaque", "application/json",
                                 b'{"done": true, "prompt_eval_count": 3, "eval_count": 4}')
        #
        assert matched.id == "ollama.native"

    def test_an_ollama_openai_compatible_path_belongs_to_openai_chat(self, registered_dialects):
        # Ollama also serves /v1/chat/completions, and there it speaks OpenAI, not its own API.
        assert registry.match("/v1/chat/completions", "application/json").id == "openai.chat"


class TestReading:
    def test_reads_the_flat_counters(self, registered_dialects):
        reading = read("ollama.native", "/api/chat", "application/x-ndjson",
                       ndjson_body([DELTA, DELTA, FINAL]))
        #
        assert reading.model_name == "llama3.2"
        assert (reading.input_tokens, reading.output_tokens) == (26, 298)

    def test_no_cache_counters_are_invented(self, registered_dialects):
        reading = read("ollama.native", "/api/chat", "application/x-ndjson",
                       ndjson_body([FINAL]))
        #
        assert reading.cache_read_tokens == 0
        assert reading.cache_creation_tokens == 0
        assert billable_input_tokens(reading) == 26

    def test_a_stream_without_a_final_line_reports_nothing(self, registered_dialects):
        # The counts only ever arrive with done: true.
        reading = read("ollama.native", "/api/chat", "application/x-ndjson",
                       ndjson_body([DELTA, DELTA]))
        #
        assert reading.input_tokens is None and reading.output_tokens is None

    def test_a_non_streamed_call_is_a_single_object(self, registered_dialects):
        reading = read("ollama.native", "/api/generate", "application/json",
                       json_body(FINAL))
        #
        assert (reading.input_tokens, reading.output_tokens) == (26, 298)

    @pytest.mark.parametrize("chunk_size", [0, 1, 7, 64, 4096])
    def test_chunking_changes_nothing(self, registered_dialects, chunk_size):
        reading = read("ollama.native", "/api/chat", "application/x-ndjson",
                       ndjson_body([DELTA, FINAL]), chunk_size)
        #
        assert (reading.input_tokens, reading.output_tokens) == (26, 298)

    def test_truncation_never_raises(self, registered_dialects):
        body = ndjson_body([DELTA, FINAL])
        #
        for cut in range(0, len(body), 7):
            read("ollama.native", "/api/chat", "application/x-ndjson", body[:cut])

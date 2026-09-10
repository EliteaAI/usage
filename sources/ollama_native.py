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

""" Ollama native API — flat counts on the final NDJSON line, no cache concept """

from .base import CACHE_EXCLUSIVE, coerce_int
from .dialect import ScannerDialect, path_of, set_if_unset, set_latest

OLLAMA_PATHS = ("/api/chat", "/api/generate", "/api/embed", "/api/embeddings")


class OllamaNativeDialect(ScannerDialect):
    """Counts are top-level scalars rather than a usage object."""

    id = "ollama.native"
    provider = "ollama"
    keys = ("prompt_eval_count", "eval_count", "model")
    cache_convention = CACHE_EXCLUSIVE

    @classmethod
    def matches(cls, endpoint, content_type, head):
        path = path_of(endpoint).rstrip("/")
        #
        if path.endswith(OLLAMA_PATHS):
            return True
        #
        return b'"prompt_eval_count"' in head or b'"eval_count"' in head

    def absorb(self, key, value):
        if key == "prompt_eval_count":
            set_if_unset(self._reading, "input_tokens", coerce_int(value))
            return
        #
        set_latest(self._reading, "output_tokens", coerce_int(value))

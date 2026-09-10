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

""" OpenAI embeddings — prompt tokens only, and a body that can be many megabytes """

from .base import CACHE_INCLUSIVE, first_int
from .dialect import ScannerDialect, path_of, set_if_unset


class OpenAIEmbeddingsDialect(ScannerDialect):
    """No completion side at all, so output is recorded as an observed zero, not None."""

    id = "openai.embeddings"
    keys = ("usage", "model")
    cache_convention = CACHE_INCLUSIVE

    def matches(self, endpoint, content_type, head):
        return path_of(endpoint).rstrip("/").endswith("/embeddings")

    def absorb(self, key, value):
        if key == "model":
            if isinstance(value, str) and value:
                set_if_unset(self._reading, "model_name", value)
            return
        #
        if not isinstance(value, dict):
            return
        #
        prompt_tokens = first_int(value, "prompt_tokens", "total_tokens")
        if prompt_tokens is None:
            return
        #
        set_if_unset(self._reading, "input_tokens", prompt_tokens)
        set_if_unset(self._reading, "output_tokens", 0)

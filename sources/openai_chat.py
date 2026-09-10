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

""" OpenAI chat completions — the shape Azure and DIAL also speak """

from .base import CACHE_INCLUSIVE, first_int
from .dialect import (
    ScannerDialect, path_of, set_if_unset, set_if_zero, set_latest,
)

CHAT_PATHS = ("/chat/completions", "/completions")


class OpenAIChatDialect(ScannerDialect):
    """`prompt_tokens` / `completion_tokens`, cached tokens inside the prompt count."""

    id = "openai.chat"
    keys = ("usage", "model")
    cache_convention = CACHE_INCLUSIVE

    def matches(self, endpoint, content_type, head):
        path = path_of(endpoint).rstrip("/")
        #
        if path.endswith(CHAT_PATHS):
            return True
        #
        return b'"prompt_tokens"' in head

    def absorb(self, key, value):
        if key == "model":
            if isinstance(value, str) and value:
                set_if_unset(self._reading, "model_name", value)
            return
        #
        if not isinstance(value, dict):
            return
        #
        set_if_unset(self._reading, "input_tokens", first_int(value, "prompt_tokens"))
        set_latest(self._reading, "output_tokens", first_int(value, "completion_tokens"))
        set_if_zero(
            self._reading, "cache_read_tokens",
            first_int(value.get("prompt_tokens_details"), "cached_tokens"),
        )
        set_if_zero(
            self._reading, "reasoning_tokens",
            first_int(value.get("completion_tokens_details"), "reasoning_tokens"),
        )

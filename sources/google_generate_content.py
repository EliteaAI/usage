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

""" Google generateContent — thinking tokens are billed as output but reported apart """

from .base import CACHE_INCLUSIVE, first_int
from .dialect import (
    ScannerDialect, path_of, set_if_unset, set_if_zero, set_latest,
)


class GoogleGenerateContentDialect(ScannerDialect):
    """`thoughtsTokenCount` sits outside `candidatesTokenCount`, so output is their sum."""

    id = "google.generate_content"
    provider = "vertex_ai"
    keys = ("usageMetadata", "modelVersion")
    cache_convention = CACHE_INCLUSIVE
    model_key = "modelVersion"

    @classmethod
    def matches(cls, endpoint, content_type, head):
        path = path_of(endpoint)
        #
        if ":generateContent" in path or ":streamGenerateContent" in path:
            return True
        #
        return b'"usageMetadata"' in head

    def absorb(self, key, value):
        if not isinstance(value, dict):
            return
        #
        set_if_unset(self._reading, "input_tokens", first_int(value, "promptTokenCount"))
        set_if_zero(
            self._reading, "cache_read_tokens", first_int(value, "cachedContentTokenCount"),
        )
        #
        candidates = first_int(value, "candidatesTokenCount")
        thoughts = first_int(value, "thoughtsTokenCount")
        #
        set_latest(self._reading, "reasoning_tokens", thoughts)
        #
        if candidates is None and thoughts is None:
            return
        #
        set_latest(self._reading, "output_tokens", (candidates or 0) + (thoughts or 0))

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

""" Anthropic messages — cached tokens are additional to input, not part of it """

from .base import CACHE_EXCLUSIVE, first_int
from .dialect import (
    ScannerDialect, path_of, set_if_unset, set_if_zero, set_latest,
)


class AnthropicMessagesDialect(ScannerDialect):
    """`message_start` reports a placeholder output count that only `message_delta` fixes."""

    id = "anthropic.messages"
    keys = ("usage", "model")
    cache_convention = CACHE_EXCLUSIVE

    @classmethod
    def matches(cls, endpoint, content_type, head):
        path = path_of(endpoint).rstrip("/")
        #
        if path.endswith("/messages") or "/messages/" in path:
            return True
        #
        return b'"cache_read_input_tokens"' in head or b'"message_start"' in head

    def absorb(self, key, value):
        if not isinstance(value, dict):
            return
        #
        set_if_unset(self._reading, "input_tokens", first_int(value, "input_tokens"))
        set_latest(self._reading, "output_tokens", first_int(value, "output_tokens"))
        set_if_zero(
            self._reading, "cache_read_tokens", first_int(value, "cache_read_input_tokens"),
        )
        set_if_zero(
            self._reading, "cache_creation_tokens",
            first_int(value, "cache_creation_input_tokens"),
        )

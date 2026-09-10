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

""" Bedrock Converse — camelCase counts, exclusive cache, binary frames when streaming """

from .base import CACHE_EXCLUSIVE, first_int
from .dialect import (
    EventStreamScannerDialect, path_of, set_if_unset, set_if_zero, set_latest,
)


def model_id_from_path(endpoint):
    """Converse bodies carry no model name; the invoked model is in the path."""
    path = path_of(endpoint)
    #
    if "/model/" not in path:
        return None
    #
    tail = path.split("/model/", 1)[1].split("/", 1)[0]
    return tail or None


class BedrockConverseDialect(EventStreamScannerDialect):
    """The `metadata` event carrying usage arrives last, after every content delta."""

    id = "bedrock.converse"
    keys = ("usage",)
    cache_convention = CACHE_EXCLUSIVE

    def matches(self, endpoint, content_type, head):
        path = path_of(endpoint).rstrip("/")
        #
        if not (path.endswith("/converse") or path.endswith("/converse-stream")):
            return False
        #
        self._use_event_stream(content_type)
        set_if_unset(self._reading, "model_name", model_id_from_path(endpoint))
        return True

    def absorb(self, key, value):
        if not isinstance(value, dict):
            return
        #
        set_if_unset(self._reading, "input_tokens", first_int(value, "inputTokens"))
        set_latest(self._reading, "output_tokens", first_int(value, "outputTokens"))
        set_if_zero(
            self._reading, "cache_read_tokens", first_int(value, "cacheReadInputTokens"),
        )
        set_if_zero(
            self._reading, "cache_creation_tokens", first_int(value, "cacheWriteInputTokens"),
        )

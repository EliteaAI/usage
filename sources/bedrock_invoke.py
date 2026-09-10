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

""" Bedrock InvokeModel — Bedrock's own metrics plus whatever body the model speaks """

import base64
import json

from .base import CACHE_EXCLUSIVE, first_int, log
from .bedrock_converse import model_id_from_path
from .dialect import (
    EventStreamScannerDialect, path_of, set_if_unset, set_if_zero, set_latest,
)

METRICS_KEY = "amazon-bedrock-invocationMetrics"


class BedrockInvokeDialect(EventStreamScannerDialect):
    """Bedrock's own metrics win; the passthrough model body is the fallback."""

    id = "bedrock.invoke"
    provider = "amazon_bedrock"
    keys = (METRICS_KEY, "usage")
    cache_convention = CACHE_EXCLUSIVE

    @classmethod
    def matches(cls, endpoint, content_type, head):
        path = path_of(endpoint).rstrip("/")
        #
        if not (path.endswith("/invoke") or path.endswith("/invoke-with-response-stream")):
            return False
        #
        return True

    def bind(self, endpoint, content_type):
        self._use_event_stream(content_type)
        set_if_unset(self._reading, "model_name", model_id_from_path(endpoint))

    def unwrap(self, payload):
        """Streamed chunks wrap the model's own JSON in a base64 `bytes` field."""
        yield payload
        #
        # Cheap reject first: every content-delta frame would otherwise be JSON-parsed twice.
        if b'"bytes"' not in payload:
            return
        #
        try:
            encoded = json.loads(payload).get("bytes")
            if isinstance(encoded, str):
                yield base64.b64decode(encoded)
        except Exception:  # pylint: disable=W0703
            log.debug("usage.sources: bedrock.invoke chunk was not a wrapped body")

    def absorb(self, key, value):
        if not isinstance(value, dict):
            return
        #
        set_if_unset(
            self._reading, "input_tokens",
            first_int(value, "inputTokenCount", "inputTokens", "input_tokens"),
        )
        set_latest(
            self._reading, "output_tokens",
            first_int(value, "outputTokenCount", "outputTokens", "output_tokens"),
        )
        set_if_zero(
            self._reading, "cache_read_tokens",
            first_int(
                value, "cacheReadInputTokenCount", "cacheReadInputTokens",
                "cache_read_input_tokens",
            ),
        )
        set_if_zero(
            self._reading, "cache_creation_tokens",
            first_int(
                value, "cacheWriteInputTokenCount", "cacheWriteInputTokens",
                "cache_creation_input_tokens",
            ),
        )

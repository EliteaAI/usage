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

""" token_source provenance, and the log severity that goes with it

This is the billing-safety signal: the drainer (#6571) can only tell a genuinely free call from
one this library failed to read by looking at `token_source`. If a failed read still claimed
`provider`, the platform would bill zero with full confidence — the exact failure mode of the
superseded usage_audit.py, one layer up.

Severity is deliberately orthogonal to provenance. An OpenAI stream sent without
`stream_options.include_usage` is *factually* unparsed but *operationally* routine, so it logs at
info; warning-level there would bury the reads that actually broke.
"""

import importlib

import pytest

from usage.sources import base, registry


class Recorder:
    """Stands in for the module log so severity can be asserted without pylon's log shape."""

    def __init__(self):
        self.calls = []

    def _record(self, level):
        def record(message, *args, **kwargs):  # pylint: disable=W0613
            self.calls.append((level, message % args if args else message))
        return record

    def __getattr__(self, level):
        return self._record(level)

    def levels(self):
        return [level for level, _ in self.calls]


@pytest.fixture
def recorder(monkeypatch):
    log = Recorder()
    # Resolved here, not at import: test_no_pylon_import re-imports the package, so a module
    # object captured at import time can be a stale twin of the one the dialects actually use.
    monkeypatch.setattr(importlib.import_module("usage.sources.dialect"), "log", log)
    return log


@pytest.fixture(autouse=True)
def registered_defaults():
    registry.clear()
    registry.register_defaults()
    yield
    registry.clear()
    registry.register_defaults()


def read(endpoint, content_type, *chunks):
    matched = registry.match(endpoint, content_type)
    for chunk in chunks:
        matched.feed(chunk)
    return matched.result()


class TestProvenance:
    def test_a_complete_read_stays_provider(self):
        body = b'{"model":"gpt-4o","usage":{"prompt_tokens":11,"completion_tokens":7}}'
        reading = read("/v1/chat/completions", "application/json", body)
        #
        assert reading.input_tokens == 11
        assert reading.token_source == base.TOKEN_SOURCE_PROVIDER

    def test_a_stream_without_include_usage_is_unparsed(self):
        # The documented OpenAI case: content arrives, a usage block never does.
        chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        reading = read("/v1/chat/completions", "text/event-stream", chunk)
        #
        assert reading.input_tokens is None
        assert reading.token_source == base.TOKEN_SOURCE_UNPARSED

    def test_a_half_read_with_failures_is_unparsed(self):
        # An SSE stream whose first usage block was readable and whose corrected one was not:
        # output is known, input never arrived, so the reading must not claim to be complete.
        body = (
            b'data: {"usage":{"completion_tokens":7}}\n\n'
            b'data: {"usage":--}\n\n'
        )
        reading = read("/v1/chat/completions", "text/event-stream", body)
        #
        assert reading.output_tokens == 7
        assert reading.token_source == base.TOKEN_SOURCE_UNPARSED

    def test_no_bytes_at_all_is_not_downgraded(self):
        # Nothing was fed, so nothing was misread — an empty reading is not a failed one.
        reading = registry.match("/v1/chat/completions", "application/json").result()
        #
        assert reading.token_source == base.TOKEN_SOURCE_PROVIDER


class TestSeverity:
    def test_the_benign_missing_usage_stream_logs_info(self, recorder):
        chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        read("/v1/chat/completions", "text/event-stream", chunk)
        #
        assert "warning" not in recorder.levels()
        assert "info" in recorder.levels()

    def test_a_broken_read_logs_warning(self, recorder):
        read(
            "/v1/chat/completions", "application/json",
            b'{"usage":{"prompt_tokens":--,"completion_tokens":7}}',
        )
        #
        assert "warning" in recorder.levels()

    def test_a_complete_read_logs_nothing(self, recorder):
        read(
            "/v1/chat/completions", "application/json",
            b'{"usage":{"prompt_tokens":11,"completion_tokens":7}}',
        )
        #
        assert recorder.calls == []


class TestEventStreamBlindSpot:
    """Raw frame bytes must count even when the framer decodes nothing.

    Counting only successfully decoded payloads left the AWS path invisible: with botocore
    missing, or the framer wedged on a corrupt frame, `_fed_bytes` stayed 0, the guard treated
    the response as empty, and every Bedrock call billed zero in silence.
    """

    def test_an_inert_framer_still_reports_unparsed(self, monkeypatch):
        matched = registry.match(
            "/model/eu.amazon.nova-pro-v1:0/converse-stream",
            "application/vnd.amazon.eventstream",
        )
        # Simulate botocore being absent — push() then decodes nothing at all.
        monkeypatch.setattr(matched._framer, "_buffer", None)  # pylint: disable=W0212
        #
        matched.feed(b"\x00" * 512)
        reading = matched.result()
        #
        assert reading.input_tokens is None
        assert reading.token_source == base.TOKEN_SOURCE_UNPARSED

    def test_a_corrupt_frame_counts_as_a_failure(self):
        matched = registry.match(
            "/model/eu.amazon.nova-pro-v1:0/converse-stream",
            "application/vnd.amazon.eventstream",
        )
        #
        matched.feed(b"\xff" * 256)
        reading = matched.result()
        #
        assert matched.failures >= 1
        assert reading.token_source == base.TOKEN_SOURCE_UNPARSED

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

""" Acceptance criterion: no response body is ever accumulated.

The code this library replaces built the whole response into a bytearray before parsing it,
on the money path, for every call. That is what turned large responses into the memory and
CPU pressure behind the platform's worst stalls. So the requirement here is structural, not
a nice-to-have: retention must stay flat no matter how long the response is.

Two complementary measurements, because each catches what the other misses:

- `retained_bytes` is cheap, so it runs over a large volume and would catch retention that
  grows slowly with the stream.
- `tracemalloc` is expensive but sees allocations the scanner does not account for itself,
  so it runs over a smaller volume as a cross-check on the first.

Throughput is asserted too. This code sits inline on every LLM response, so a scanner slow
enough to burn seconds of CPU on a large body is the same gevent-starvation failure as the
buffer it replaces — a correctness-preserving rewrite could silently reintroduce it.
"""

import json
import time
import tracemalloc

import pytest

from usage.sources import registry
from usage.sources.framing import JSONValueScanner

# Large enough that body-sized retention would be unmistakable, small enough to stay quick.
VOLUME_BYTES = 192 * 1024 * 1024
TRACED_BYTES = 8 * 1024 * 1024
RETENTION_CEILING = 1024 * 1024

# Generous by two orders of magnitude against measured throughput; this is a floor, not a
# benchmark, so it should only ever fire on a genuine regression to per-byte scanning.
MIN_THROUGHPUT_MB_S = 20

STREAMING_ENDPOINTS = [
    ("/v1/chat/completions", "text/event-stream"),
    ("/v1/messages", "text/event-stream"),
    ("/v1/responses", "text/event-stream"),
    ("/api/chat", "application/x-ndjson"),
    ("/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse", "text/event-stream"),
]


def sse_chunk():
    """One realistically-sized streamed content delta."""
    return b"data: " + json.dumps({
        "id": "chatcmpl-x", "object": "chat.completion.chunk", "model": "gpt-4o",
        "choices": [{"index": 0, "delta": {"content": "lorem ipsum dolor sit amet " * 40}}],
    }).encode() + b"\n\n"


def push(target, chunk, total_bytes):
    """Stream a chunk repeatedly, returning bytes pushed and peak retention."""
    pushed, peak = 0, 0
    #
    while pushed < total_bytes:
        target.feed(chunk)
        pushed += len(chunk)
        peak = max(peak, target.retained_bytes)
    #
    return pushed, peak


class TestScannerRetention:
    """The scanner is what every text dialect delegates to."""

    def test_retention_stays_flat_over_a_very_long_stream(self):
        scanner = JSONValueScanner(("usage", "model"))
        chunk = sse_chunk()
        #
        pushed, peak = push(scanner, chunk, VOLUME_BYTES)
        #
        assert pushed >= VOLUME_BYTES
        assert peak < RETENTION_CEILING, f"retained {peak} bytes after {pushed}"

    def test_throughput_stays_off_the_per_byte_floor(self):
        scanner = JSONValueScanner(("usage", "model"))
        chunk = sse_chunk()
        #
        started = time.perf_counter()
        pushed, _ = push(scanner, chunk, VOLUME_BYTES)
        elapsed = time.perf_counter() - started
        #
        rate = pushed / 1048576 / max(elapsed, 1e-9)
        assert rate > MIN_THROUGHPUT_MB_S, f"{rate:.1f} MB/s over {pushed} bytes"

    def test_a_single_enormous_unlisted_string_is_not_retained(self):
        # A base64 image or a giant tool result arrives as one string value. It must be
        # discarded as it streams past, not held until its closing quote.
        scanner = JSONValueScanner(("usage",))
        peak = 0
        #
        scanner.feed(b'{"content": "')
        for _ in range(2048):
            scanner.feed(b"A" * 65536)
            peak = max(peak, scanner.retained_bytes)
        scanner.feed(b'", "usage": {"prompt_tokens": 3}}')
        #
        assert peak < 4096
        assert list(scanner.close()) == []


class TestDialectRetention:
    """End to end through the real dispatch path."""

    @pytest.mark.parametrize("endpoint,content_type", STREAMING_ENDPOINTS)
    def test_retention_does_not_track_body_size(
        self, registered_dialects, endpoint, content_type,
    ):
        dialect = registry.match(endpoint, content_type)
        #
        pushed, peak = push(dialect, sse_chunk(), VOLUME_BYTES)
        #
        assert peak < RETENTION_CEILING, f"{endpoint}: retained {peak} over {pushed} pushed"

    @pytest.mark.parametrize("endpoint,content_type", STREAMING_ENDPOINTS)
    def test_traced_allocation_does_not_track_body_size(
        self, registered_dialects, endpoint, content_type,
    ):
        # Cross-check on the test above: tracemalloc sees allocations the scanner does not
        # count itself, so a hidden buffer somewhere else in the dialect still shows up.
        dialect = registry.match(endpoint, content_type)
        chunk = sse_chunk()
        #
        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            pushed, _ = push(dialect, chunk, TRACED_BYTES)
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        #
        assert peak - baseline < RETENTION_CEILING, (
            f"{endpoint}: peaked at {peak - baseline} bytes over {pushed} pushed"
        )
        assert current - baseline < RETENTION_CEILING

    def test_usage_is_still_found_at_the_end_of_a_huge_stream(self, registered_dialects):
        # Bounded retention is worthless if it costs correctness: the counts still have to
        # come out after megabytes of content the scanner threw away.
        dialect = registry.match("/v1/chat/completions", "text/event-stream")
        chunk = sse_chunk()
        #
        for _ in range(4000):
            dialect.feed(chunk)
        dialect.feed(
            b'data: ' + json.dumps({
                "model": "gpt-4o", "choices": [],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12,
                          "prompt_tokens_details": {"cached_tokens": 8}},
            }).encode() + b'\n\ndata: [DONE]\n\n'
        )
        reading = dialect.result()
        #
        assert (reading.input_tokens, reading.output_tokens) == (40, 12)
        assert reading.cache_read_tokens == 8


class TestEventStreamRetention:
    """Bedrock's binary envelope holds one incomplete frame, never the stream."""

    def test_traced_allocation_does_not_track_stream_size(self, registered_dialects):
        eventstream_builder = pytest.importorskip("eventstream_builder")
        pytest.importorskip("botocore")
        #
        dialect = registry.match(
            "/model/eu.amazon.nova-pro-v1:0/converse-stream",
            "application/vnd.amazon.eventstream",
        )
        frame = eventstream_builder.encode_event("contentBlockDelta", {
            "contentBlockIndex": 0, "delta": {"text": "lorem ipsum dolor sit amet " * 40},
        })
        pushed = 0
        #
        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            while pushed < TRACED_BYTES:
                dialect.feed(frame)
                pushed += len(frame)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        #
        assert peak - baseline < RETENTION_CEILING, (
            f"peaked at {peak - baseline} bytes over {pushed} pushed"
        )
        #
        dialect.feed(eventstream_builder.encode_event("metadata", {
            "usage": {"inputTokens": 220, "outputTokens": 75},
        }))
        assert dialect.result().input_tokens == 220

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

""" AWS binary event-stream framing, decoded by botocore.

Bedrock is the one dialect family whose envelope is not text, so it is the one place real
frame decoding is unavoidable. botocore ships the decoder; fixtures/eventstream_builder.py
supplies the encoder botocore lacks. That split is what lets these tests run with no AWS
credentials at all — the framing is synthesized locally and only the JSON payloads inside it
come from the provider's documented schema.
"""

import json

import pytest

import eventstream_builder

from usage.sources.framing import EventStreamFramer

pytest.importorskip("botocore", reason="botocore supplies the event-stream decoder")

EVENTS = [
    ("messageStart", {"role": "assistant"}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hello"}}),
    ("messageStop", {"stopReason": "end_turn"}),
    ("metadata", {"usage": {"inputTokens": 220, "outputTokens": 75}}),
]


def payloads(framer, stream, chunk_size=0):
    """Push a stream through the framer and collect the decoded payloads."""
    out = []
    #
    if chunk_size <= 0:
        out.extend(payload for _, payload in framer.push(stream))
    else:
        for offset in range(0, len(stream), chunk_size):
            out.extend(
                payload for _, payload in framer.push(stream[offset:offset + chunk_size])
            )
    #
    return out


class TestDecoding:
    """Frames in, payloads out, headers intact."""

    def test_all_frames_decode(self):
        stream = eventstream_builder.encode_stream(EVENTS)
        #
        decoded = [json.loads(payload) for payload in payloads(EventStreamFramer(), stream)]
        assert decoded == [body for _, body in EVENTS]

    def test_event_type_header_survives(self):
        stream = eventstream_builder.encode_stream(EVENTS)
        #
        headers = [headers for headers, _ in EventStreamFramer().push(stream)]
        assert [header[":event-type"] for header in headers] == [
            "messageStart", "contentBlockDelta", "messageStop", "metadata",
        ]

    def test_usage_is_in_the_last_frame(self):
        # Bedrock puts the metadata event after every content delta, so a dialect that
        # stopped reading early would find no usage at all.
        stream = eventstream_builder.encode_stream(EVENTS)
        #
        last = json.loads(payloads(EventStreamFramer(), stream)[-1])
        assert last["usage"]["inputTokens"] == 220


class TestChunkBoundaries:
    """A frame split across TCP reads is the normal case, not the edge case."""

    @pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 16, 17, 64, 128, 1024])
    def test_fixed_chunk_sizes(self, chunk_size):
        stream = eventstream_builder.encode_stream(EVENTS)
        #
        decoded = [
            json.loads(payload)
            for payload in payloads(EventStreamFramer(), stream, chunk_size)
        ]
        assert decoded == [body for _, body in EVENTS]

    @pytest.mark.parametrize("split", range(0, 240, 7))
    def test_two_way_splits_across_the_prelude_and_headers(self, split):
        # Splits below ~240 bytes land inside the first frame's prelude, CRC and headers —
        # the parts that carry length information the decoder needs before it can proceed.
        stream = eventstream_builder.encode_stream(EVENTS)
        framer = EventStreamFramer()
        #
        out = [payload for _, payload in framer.push(stream[:split])]
        out.extend(payload for _, payload in framer.push(stream[split:]))
        #
        assert [json.loads(payload) for payload in out] == [body for _, body in EVENTS]

    def test_a_truncated_final_frame_is_simply_absent(self):
        # A stream that dies mid-frame yields the complete frames and nothing more; the
        # caller sees the counts it did receive and marks the rest unobserved.
        stream = eventstream_builder.encode_stream(EVENTS)
        framer = EventStreamFramer()
        #
        out = payloads(framer, stream[:-30])
        assert len(out) == len(EVENTS) - 1


class TestCorruption:
    """Corrupt binary must degrade quietly — this is the money path."""

    def test_a_bad_crc_does_not_raise(self):
        stream = eventstream_builder.corrupt_last_crc(
            eventstream_builder.encode_stream(EVENTS)
        )
        framer = EventStreamFramer()
        #
        out = payloads(framer, stream)
        #
        # botocore rejects the corrupt frame; the framer swallows the error and stops.
        # Whether earlier frames survived is decoder detail, but nothing may propagate.
        assert len(out) <= len(EVENTS)

    @pytest.mark.parametrize("body", [
        b"",
        b"\x00" * 16,
        b"\xff" * 64,
        b"not an event stream at all",
        json.dumps({"usage": {"inputTokens": 1}}).encode(),
    ])
    def test_junk_input_does_not_raise(self, body):
        payloads(EventStreamFramer(), body)

    def test_the_framer_stays_inert_after_breaking(self):
        # Once the byte stream has desynchronised there is no way back, so the framer must
        # stop rather than reinterpret arbitrary offsets as frame preludes.
        framer = EventStreamFramer()
        payloads(framer, b"\xff" * 4096)
        #
        assert payloads(framer, eventstream_builder.encode_stream(EVENTS)) == []


class TestBoundedRetention:
    """Only the incomplete frame is held, never the stream."""

    def test_many_frames_do_not_accumulate(self):
        framer = EventStreamFramer()
        total = 0
        #
        for index in range(5000):
            stream = eventstream_builder.encode_event(
                "contentBlockDelta",
                {"contentBlockIndex": 0, "delta": {"text": f"chunk {index}"}},
            )
            total += len(list(framer.push(stream)))
        #
        assert total == 5000

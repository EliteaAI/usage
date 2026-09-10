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

""" Test-only encoder for the AWS binary event-stream framing.

botocore ships a decoder but no encoder, so fixtures for the Bedrock dialects have to
synthesize frames. Building them here rather than checking in captured binary blobs means
the framing tests need no AWS credentials at all — only the *payloads* come from a live
capture, and those are plain JSON.

Wire format, all integers big-endian:

    total_length (4) | headers_length (4) | prelude_crc32 (4)
    headers (headers_length)
    payload (total_length - headers_length - 16)
    message_crc32 (4)          # over every byte before it

A header is: name_length (1) | name | value_type (1) | value_length (2) | value,
where value_type 7 is a UTF-8 string — the only type Bedrock uses for event metadata.
"""

import binascii
import json
import struct

_HEADER_TYPE_STRING = 7


def encode_headers(headers):
    """Encode a flat {name: str} mapping as event-stream string headers."""
    out = bytearray()
    #
    for name, value in headers.items():
        name_bytes = name.encode("utf-8")
        value_bytes = str(value).encode("utf-8")
        #
        out += struct.pack("!B", len(name_bytes)) + name_bytes
        out += struct.pack("!B", _HEADER_TYPE_STRING)
        out += struct.pack("!H", len(value_bytes)) + value_bytes
    #
    return bytes(out)


def encode_frame(payload, headers=None):
    """One complete event-stream message, ready to hand to EventStreamBuffer.add_data()."""
    header_bytes = encode_headers(headers or {})
    total_length = len(header_bytes) + len(payload) + 16
    #
    prelude = struct.pack("!II", total_length, len(header_bytes))
    prelude += struct.pack("!I", binascii.crc32(prelude) & 0xFFFFFFFF)
    #
    message = prelude + header_bytes + payload
    return message + struct.pack("!I", binascii.crc32(message) & 0xFFFFFFFF)


def encode_event(event_type, body):
    """A Bedrock-shaped event: JSON payload plus the `:event-type` header it arrives with."""
    return encode_frame(
        json.dumps(body).encode("utf-8"),
        {
            ":event-type": event_type,
            ":message-type": "event",
            ":content-type": "application/json",
        },
    )


def encode_stream(events):
    """Concatenate (event_type, body) pairs into one response body."""
    return b"".join(encode_event(event_type, body) for event_type, body in events)


def corrupt_last_crc(stream):
    """Flip the trailing message CRC so the decoder must reject the final frame."""
    broken = bytearray(stream)
    broken[-1] ^= 0xFF
    return bytes(broken)

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

""" Envelope handling — pull the interesting JSON values out without holding the stream """

import json

from .base import log

MAX_KEY_LENGTH = 64
MAX_VALUE_BYTES = 64 * 1024

_QUOTE = 0x22
_BACKSLASH = 0x5C
_COLON = 0x3A
_OPEN_BRACE = 0x7B
_CLOSE_BRACE = 0x7D
_OPEN_BRACKET = 0x5B
_CLOSE_BRACKET = 0x5D
_WHITESPACE = frozenset(b" \t\n\r")
_SCALAR_END = frozenset(b",}] \t\n\r")

_OPENERS = frozenset((_OPEN_BRACE, _OPEN_BRACKET))
_CLOSERS = frozenset((_CLOSE_BRACE, _CLOSE_BRACKET))


class JSONValueScanner:
    """Emits (key, parsed_value) for whitelisted keys as each value completes.

    Envelope-agnostic: SSE, NDJSON, a JSON array and a plain body all carry the same literal
    keys, so one scanner covers every text dialect and nothing has to buffer a body just to
    reach a `usage` block sitting behind a huge `data` array.

    Dispatch is by `bytes.find` on the literal `"key"` token rather than a per-byte state
    machine, because this runs inline on every LLM response and a Python byte loop caps out
    around 2 MB/s. JSON escapes every quote inside a string, so that token can only be a
    real key or a whole string value — the colon check below tells those two apart.
    """

    def __init__(self, keys, label=None):
        # `label` is the owning dialect id — the only way a log line names the provider.
        self._label = label or "?"
        self._patterns = [
            (b'"' + key.encode("utf-8") + b'"', key)
            for key in keys if len(key) <= MAX_KEY_LENGTH
        ]
        self._overlap = max((len(pattern) for pattern, _ in self._patterns), default=1) - 1
        self._buffer = bytearray()
        self._pos = 0
        self._capture = None
        self._capture_key = None
        self._capture_mode = None
        self._capture_depth = 0
        self._capture_in_string = False
        self._capture_escaped = False
        self.failures = 0

    def feed(self, chunk):
        """Bytes in, completed (key, value) pairs out. Never raises on malformed input."""
        emitted = []
        if not chunk:
            return emitted
        #
        try:
            self._buffer += chunk
            self._scan(emitted)
        except Exception:  # pylint: disable=W0703
            self.failures += 1
            log.warning(
                "usage.sources: %s scanner aborted mid-value (key=%r, held=%d bytes)",
                self._label, self._capture_key, self.retained_bytes, exc_info=True,
            )
            self._reset_capture()
            self._pos = len(self._buffer)
        #
        self._trim()
        return emitted

    def close(self):
        """Flush a trailing unterminated scalar (a body ending mid-number)."""
        if self._capture is None or self._capture_mode != "scalar":
            return []
        #
        emitted = []
        self._finish(emitted)
        return emitted

    def _scan(self, emitted):
        while True:
            if self._capture is not None:
                if not self._grow(emitted):
                    return
                continue
            #
            index, key, width = self._find_key()
            #
            if index is None:
                # No whole token can start before the tail overlap, so drop everything else.
                self._pos = max(self._pos, len(self._buffer) - self._overlap)
                return
            #
            cursor = self._skip_whitespace(index + width)
            if cursor is None:
                self._pos = index
                return
            #
            if self._buffer[cursor] != _COLON:
                self._pos = index + 1
                continue
            #
            cursor = self._skip_whitespace(cursor + 1)
            if cursor is None:
                self._pos = index
                return
            #
            self._begin(key, cursor)

    def _find_key(self):
        """Earliest whitelisted key token at or after the cursor."""
        best, best_key, best_width = None, None, 0
        #
        for pattern, key in self._patterns:
            index = self._buffer.find(pattern, self._pos)
            if index >= 0 and (best is None or index < best):
                best, best_key, best_width = index, key, len(pattern)
        #
        return best, best_key, best_width

    def _skip_whitespace(self, cursor):
        """First non-whitespace index, or None when the chunk ran out first."""
        end = len(self._buffer)
        #
        while cursor < end and self._buffer[cursor] in _WHITESPACE:
            cursor += 1
        #
        return None if cursor >= end else cursor

    def _begin(self, key, cursor):
        byte = self._buffer[cursor]
        #
        self._capture = bytearray()
        self._capture_key = key
        self._pos = cursor
        #
        if byte in _OPENERS:
            self._capture_mode = "container"
        elif byte == _QUOTE:
            self._capture_mode = "string"
        else:
            self._capture_mode = "scalar"

    def _grow(self, emitted):
        """Consume the value in progress. False means it needs more bytes."""
        if self._capture_mode == "scalar":
            return self._grow_scalar(emitted)
        #
        return self._grow_structured(emitted)

    def _grow_scalar(self, emitted):
        buffer, end = self._buffer, len(self._buffer)
        index = self._pos
        #
        while index < end and buffer[index] not in _SCALAR_END:
            index += 1
        #
        self._capture += buffer[self._pos:index]
        self._pos = index
        #
        if index < end:
            self._finish(emitted)
            return True
        #
        return self._abandon_if_oversized()

    def _grow_structured(self, emitted):
        buffer, end = self._buffer, len(self._buffer)
        index, done = self._pos, False
        #
        while index < end:
            byte = buffer[index]
            index += 1
            #
            if self._capture_escaped:
                self._capture_escaped = False
            elif byte == _BACKSLASH and self._capture_in_string:
                self._capture_escaped = True
            elif byte == _QUOTE:
                self._capture_in_string = not self._capture_in_string
                if self._capture_mode == "string" and not self._capture_in_string:
                    done = True
                    break
            elif self._capture_in_string:
                continue
            elif byte in _OPENERS:
                self._capture_depth += 1
            elif byte in _CLOSERS:
                self._capture_depth -= 1
                if self._capture_depth <= 0:
                    done = True
                    break
        #
        self._capture += buffer[self._pos:index]
        self._pos = index
        #
        if done:
            self._finish(emitted)
            return True
        #
        return self._abandon_if_oversized()

    def _abandon_if_oversized(self):
        """A pathological value must not become an unbounded buffer."""
        if len(self._capture) <= MAX_VALUE_BYTES:
            return False
        #
        self.failures += 1
        log.warning(
            "usage.sources: %s abandoned key %r — value exceeded %d bytes",
            self._label, self._capture_key, MAX_VALUE_BYTES,
        )
        self._reset_capture()
        return True

    def _finish(self, emitted):
        key, raw = self._capture_key, bytes(self._capture)
        self._reset_capture()
        #
        try:
            emitted.append((key, json.loads(raw)))
        except (ValueError, TypeError):
            self.failures += 1
            log.warning(
                "usage.sources: %s key %r held %d bytes that were not valid JSON",
                self._label, key, len(raw),
            )

    def _reset_capture(self):
        self._capture = None
        self._capture_key = None
        self._capture_mode = None
        self._capture_depth = 0
        self._capture_in_string = False
        self._capture_escaped = False

    def _trim(self):
        """Drop consumed bytes — this is what keeps retention flat over a long stream."""
        if self._pos <= 0:
            return
        #
        del self._buffer[:self._pos]
        self._pos = 0

    @property
    def retained_bytes(self):
        """What the scanner is holding — asserted by the no-buffering test."""
        return len(self._buffer) + (len(self._capture) if self._capture is not None else 0)


class EventStreamFramer:
    """AWS binary event-stream frames, decoded by botocore. Yields (headers, payload)."""

    def __init__(self, label=None):
        self._label = label or "?"
        self._buffer = None
        self._broken = False
        self.failures = 0
        #
        try:
            from botocore.eventstream import EventStreamBuffer  # pylint: disable=C0415,E0401
            self._buffer = EventStreamBuffer()
        except ImportError:
            log.warning(
                "usage.sources: %s inert — botocore unavailable, AWS frames cannot be decoded",
                self._label,
            )

    def push(self, chunk):
        """Bytes in, complete events out. A corrupt frame stops decoding, it never raises."""
        if self._buffer is None or self._broken or not chunk:
            return []
        #
        events = []
        try:
            self._buffer.add_data(bytes(chunk))
            for message in self._buffer:
                events.append((dict(message.headers), message.payload))
        except Exception:  # pylint: disable=W0703
            self._broken = True
            self.failures += 1
            log.warning(
                "usage.sources: %s event-stream decode failed, no further frames read",
                self._label, exc_info=True,
            )
        #
        return events

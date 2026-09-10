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

""" Shared skeleton and merge rules for the concrete dialects """

from .base import CACHE_EXCLUSIVE, UsageReading, log
from .framing import EventStreamFramer, JSONValueScanner


def path_of(endpoint):
    """Endpoint path without the query string."""
    return (endpoint or "").split("?", 1)[0]


def query_of(endpoint):
    parts = (endpoint or "").split("?", 1)
    return parts[1] if len(parts) > 1 else ""


def is_json(content_type):
    return "json" in (content_type or "").lower()


def set_if_unset(reading, field, value):
    """First observation wins — prompt counts are reported once and repeated verbatim."""
    if value is not None and getattr(reading, field) is None:
        setattr(reading, field, value)


def set_if_zero(reading, field, value):
    """First non-zero observation wins, for the fields that default to 0."""
    if value and not getattr(reading, field):
        setattr(reading, field, value)


def set_latest(reading, field, value):
    """Latest observation wins — streamed output counts are corrected as they grow."""
    if value is not None:
        setattr(reading, field, value)


class ScannerDialect:
    """A text dialect: a whitelisted key scanner plus dialect-specific merge rules."""

    id = None
    keys = ()
    cache_convention = CACHE_EXCLUSIVE

    def __init__(self):
        self._scanner = JSONValueScanner(self.keys, label=self.id)
        self._reading = UsageReading(
            cache_convention=self.cache_convention, dialect=self.id,
        )
        self._fed_bytes = 0
        self._absorb_failures = 0

    def matches(self, endpoint, content_type, head):
        raise NotImplementedError

    def feed(self, chunk):
        self._fed_bytes += len(chunk) if chunk else 0
        for key, value in self._scanner.feed(chunk):
            self._absorb(key, value)

    def result(self):
        for key, value in self._scanner.close():
            self._absorb(key, value)
        #
        self._warn_if_nothing_observed()
        return self._reading

    def _warn_if_nothing_observed(self):
        """Bytes went in and no counts came out — the case that silently costs money."""
        if not self._fed_bytes:
            return
        if self._reading.input_tokens is not None or self._reading.output_tokens is not None:
            return
        #
        log.warning(
            "usage.sources: %s read no token counts from %d bytes "
            "(scanner failures=%d, absorb failures=%d)",
            self.id, self._fed_bytes, self._scanner.failures, self._absorb_failures,
        )

    def _absorb(self, key, value):
        try:
            self.absorb(key, value)
        except Exception:  # pylint: disable=W0703
            self._absorb_failures += 1
            log.warning("usage.sources: %s could not absorb %r", self.id, key, exc_info=True)

    def absorb(self, key, value):
        raise NotImplementedError

    @property
    def retained_bytes(self):
        """Bytes still held mid-stream — the no-buffering guarantee, made observable."""
        return self._scanner.retained_bytes


class EventStreamScannerDialect(ScannerDialect):
    """A dialect whose bytes arrive either as AWS binary frames or as a plain JSON body."""

    def __init__(self):
        super().__init__()
        self._framer = None

    def _use_event_stream(self, content_type):
        """Called from matches() — the registry hands the matched instance to the caller."""
        if "eventstream" in (content_type or "").lower():
            self._framer = EventStreamFramer(label=self.id)

    def feed(self, chunk):
        if self._framer is None:
            super().feed(chunk)
            return
        #
        for _, payload in self._framer.push(chunk):
            for inner in self.unwrap(payload):
                super().feed(inner)

    def unwrap(self, payload):
        """Frame payloads worth scanning — overridden where usage hides one level deeper."""
        yield payload

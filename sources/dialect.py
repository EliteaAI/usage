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

from .base import (
    CACHE_EXCLUSIVE, TOKEN_SOURCE_UNPARSED, UsageReading, log,
)
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
    # The key carrying the model name — Google spells it `modelVersion`.
    model_key = "model"

    def __init__(self):
        self._scanner = JSONValueScanner(self.keys, label=self.id)
        self._reading = UsageReading(
            cache_convention=self.cache_convention, dialect=self.id,
        )
        self._fed_bytes = 0
        self._pushed_bytes = 0
        self._absorb_failures = 0
        self._keys_seen = 0

    @classmethod
    def matches(cls, endpoint, content_type, head):
        """Predicate only — must not touch instance state, the registry probes the class."""
        raise NotImplementedError

    def bind(self, endpoint, content_type):
        """Per-response setup that needs the request context. Called once, before feed()."""

    def feed(self, chunk):
        self._fed_bytes += len(chunk) if chunk else 0
        for key, value in self._scanner.feed(chunk):
            self._absorb(key, value)

    def result(self):
        for key, value in self._scanner.close():
            self._absorb(key, value)
        #
        self._finalize_token_source()
        return self._reading

    @property
    def failures(self):
        """Everything that went wrong while reading this response."""
        return self._scanner.failures + self._absorb_failures + self._framer_failures()

    def _framer_failures(self):
        return 0

    def _finalize_token_source(self):
        """Mark the reading unparsed whenever we owe the caller a number we do not have.

        The caller cannot otherwise tell a genuinely free call from one we failed to read,
        which is the whole difference between a correct bill and a confidently wrong one.
        """
        if not (self._fed_bytes or self._pushed_bytes):
            return
        #
        missing = self._reading.input_tokens is None or self._reading.output_tokens is None
        nothing = self._reading.input_tokens is None and self._reading.output_tokens is None
        #
        # `missing` alone is enough: a half-read (e.g. a safety-blocked Gemini response with
        # promptTokenCount but no candidatesTokenCount) must not stay "provider" just because
        # nothing technically raised.
        if not missing:
            return
        #
        self._reading.token_source = TOKEN_SOURCE_UNPARSED
        self._log_unparsed(nothing)

    def _log_unparsed(self, nothing):
        """A stream that never reported usage is routine; a broken read is not."""
        detail = (
            "bytes=%d/%d, keys=%d, scanner=%d, absorb=%d, framer=%d" % (
                self._fed_bytes, self._pushed_bytes, self._keys_seen,
                self._scanner.failures, self._absorb_failures, self._framer_failures(),
            )
        )
        #
        # No usage keys and nothing broken is the documented OpenAI-without-include_usage
        # case — warning-level here would bury the reads that actually failed.
        if nothing and not self._keys_seen and not self.failures:
            log.info(
                "usage.sources: %s response carried no usage keys (%s)", self.id, detail,
            )
            return
        #
        log.warning(
            "usage.sources: %s could not read complete token counts (%s)", self.id, detail,
        )

    def _absorb(self, key, value):
        try:
            if key == self.model_key:
                self._absorb_model(value)
                return
            #
            # Only usage-bearing keys count here — every OpenAI SSE chunk carries "model",
            # so counting it too would make a stream-without-usage look like a broken read.
            self._keys_seen += 1
            self.absorb(key, value)
        except Exception:  # pylint: disable=W0703
            self._absorb_failures += 1
            log.warning("usage.sources: %s could not absorb %r", self.id, key, exc_info=True)

    def _absorb_model(self, value):
        if isinstance(value, str) and value:
            set_if_unset(self._reading, "model_name", value)

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
        if "eventstream" in (content_type or "").lower():
            self._framer = EventStreamFramer(label=self.id)

    def _framer_failures(self):
        return self._framer.failures if self._framer is not None else 0

    def feed(self, chunk):
        if self._framer is None:
            super().feed(chunk)
            return
        #
        # Raw frame bytes counted separately: an inert or wedged framer decodes nothing, and
        # counting only decoded payloads would leave that case looking like an empty response.
        self._pushed_bytes += len(chunk) if chunk else 0
        #
        for _, payload in self._framer.push(chunk):
            for inner in self.unwrap(payload):
                super().feed(inner)

    def unwrap(self, payload):
        """Frame payloads worth scanning — overridden where usage hides one level deeper."""
        yield payload

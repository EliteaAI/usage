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

""" The envelope-agnostic key scanner: the one component every text dialect leans on.

There is deliberately no SSEFramer / NDJSONFramer / JSONArrayFramer here. SSE, NDJSON, a
JSON array and a plain body all carry the same literal keys in the same byte stream, so
splitting on envelope boundaries first buys nothing and costs a buffer per envelope. One
scanner that watches for whitelisted keys covers all four, which is why the envelope column
of a fixture only decides how the test builds the bytes — never how they are parsed.
"""

import json

import pytest

from usage.sources.framing import MAX_KEY_LENGTH, MAX_VALUE_BYTES, JSONValueScanner


def drain(scanner, body, chunk_size=0):
    """Feed a body and collect every (key, value) the scanner emits."""
    out = []
    #
    if chunk_size <= 0:
        out.extend(scanner.feed(body))
    else:
        for offset in range(0, len(body), chunk_size):
            out.extend(scanner.feed(body[offset:offset + chunk_size]))
    #
    out.extend(scanner.close())
    return out


class TestBasicExtraction:
    """Whitelisted keys come out parsed; everything else is ignored."""

    def test_object_value(self):
        body = b'{"id": "x", "usage": {"prompt_tokens": 7}, "model": "gpt-4o"}'
        #
        assert drain(JSONValueScanner(("usage", "model")), body) == [
            ("usage", {"prompt_tokens": 7}),
            ("model", "gpt-4o"),
        ]

    def test_scalar_values(self):
        body = b'{"prompt_eval_count": 100, "eval_count": 50, "done": true}'
        #
        assert drain(JSONValueScanner(("prompt_eval_count", "eval_count")), body) == [
            ("prompt_eval_count", 100),
            ("eval_count", 50),
        ]

    def test_unlisted_keys_are_dropped(self):
        body = b'{"choices": [{"message": {"content": "long text"}}], "usage": {"a": 1}}'
        #
        assert drain(JSONValueScanner(("usage",)), body) == [("usage", {"a": 1})]

    def test_nested_occurrence_is_found(self):
        # Responses streaming nests usage inside a response object, and Anthropic nests it
        # inside message. Depth must not matter.
        body = b'{"type": "x", "response": {"id": "r", "usage": {"input_tokens": 3}}}'
        #
        assert drain(JSONValueScanner(("usage",)), body) == [("usage", {"input_tokens": 3})]

    def test_every_occurrence_is_emitted_in_order(self):
        # Streaming means the same key arrives many times with growing counts; the merge
        # rules upstream need all of them, in order.
        body = (
            b'{"usage": {"output_tokens": 1}}\n'
            b'{"usage": {"output_tokens": 5}}\n'
            b'{"usage": {"output_tokens": 9}}\n'
        )
        #
        values = [value["output_tokens"] for _, value in drain(JSONValueScanner(("usage",)), body)]
        assert values == [1, 5, 9]

    def test_a_string_only_counts_as_a_key_when_a_colon_follows(self):
        # "usage" appearing as a *value* must not be mistaken for a key, or a chat message
        # mentioning the word would inject a bogus reading.
        body = b'{"content": "usage", "note": "usage", "usage": {"prompt_tokens": 2}}'
        #
        assert drain(JSONValueScanner(("usage",)), body) == [("usage", {"prompt_tokens": 2})]

    def test_whitespace_between_key_and_value(self):
        body = b'{"usage"   :   {"prompt_tokens": 4}}'
        #
        assert drain(JSONValueScanner(("usage",)), body) == [("usage", {"prompt_tokens": 4})]


class TestEnvelopeIndependence:
    """Same keys, four envelopes, identical output."""

    payload = {"prompt_tokens": 40, "completion_tokens": 12}

    def _values(self, body):
        return [value for _, value in drain(JSONValueScanner(("usage",)), body)]

    def test_plain_json(self):
        body = json.dumps({"usage": self.payload}).encode()
        assert self._values(body) == [self.payload]

    def test_sse(self):
        body = (
            b"data: " + json.dumps({"choices": []}).encode() + b"\n\n"
            b"data: " + json.dumps({"usage": self.payload}).encode() + b"\n\n"
            b"data: [DONE]\n\n"
        )
        assert self._values(body) == [self.payload]

    def test_ndjson(self):
        body = (
            json.dumps({"done": False}).encode() + b"\n"
            + json.dumps({"usage": self.payload}).encode() + b"\n"
        )
        assert self._values(body) == [self.payload]

    def test_json_array(self):
        # Gemini's non-SSE streaming body is one big array whose last element holds the
        # final usageMetadata. Nothing may buffer the array to reach it.
        body = json.dumps([{"candidates": []}, {"usage": self.payload}]).encode()
        assert self._values(body) == [self.payload]


class TestChunkBoundaries:
    """Every possible split point must give the same answer."""

    body = (
        b'data: {"model": "gpt-4o", "choices": [{"delta": {"content": "hi"}}]}\n\n'
        b'data: {"model": "gpt-4o", "usage": {"prompt_tokens": 40, '
        b'"completion_tokens": 12, "prompt_tokens_details": {"cached_tokens": 8}}}\n\n'
        b'data: [DONE]\n\n'
    )
    expected = [
        ("model", "gpt-4o"),
        ("model", "gpt-4o"),
        ("usage", {
            "prompt_tokens": 40, "completion_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 8},
        }),
    ]

    def test_single_feed(self):
        assert drain(JSONValueScanner(("usage", "model")), self.body) == self.expected

    @pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 11, 13, 29, 64, 256])
    def test_fixed_chunk_sizes(self, chunk_size):
        assert drain(
            JSONValueScanner(("usage", "model")), self.body, chunk_size,
        ) == self.expected

    @pytest.mark.parametrize("split", range(0, len(body) + 1, 3))
    def test_every_third_split_point(self, split):
        # Exhaustive over ~60 split points: the interesting ones are mid-key, mid-colon,
        # mid-number and mid-nested-object, and this sweep hits all of them.
        scanner = JSONValueScanner(("usage", "model"))
        out = list(scanner.feed(self.body[:split]))
        out.extend(scanner.feed(self.body[split:]))
        out.extend(scanner.close())
        #
        assert out == self.expected

    @pytest.mark.parametrize("split", [
        len(b'data: {"model": "gpt-'),
        len(b'data: {"model": "gpt-4o"'),
        len(b'data: {"model": "gpt-4o":'),
    ])
    def test_splits_inside_a_key_or_colon(self, split):
        scanner = JSONValueScanner(("usage", "model"))
        out = list(scanner.feed(self.body[:split]))
        out.extend(scanner.feed(self.body[split:]))
        out.extend(scanner.close())
        #
        assert out == self.expected


class TestMalformedInput:
    """A dialect never raises, so the scanner underneath it never raises either."""

    @pytest.mark.parametrize("body", [
        b'{"usage": {"prompt_tokens": 5',
        b'{"usage": ',
        b'{"usage": {"prompt_tokens": }}',
        b'{"usage": nul}',
        b"",
        b"\x00\x01\x02\xff\xfe",
        b'{"usage": {"a": "\xff\xfe invalid utf8"}}',
        b'data: [DONE]\n\n',
        b'not json at all',
    ])
    def test_nothing_raises(self, body):
        scanner = JSONValueScanner(("usage",))
        #
        drain(scanner, body)

    def test_truncated_value_yields_nothing_rather_than_a_guess(self):
        # Half a usage block is not a usage block. Emitting a partial parse here would
        # bill a number the provider never sent.
        assert drain(JSONValueScanner(("usage",)), b'{"usage": {"prompt_tokens": 5') == []

    def test_a_valid_value_before_garbage_survives(self):
        body = b'{"usage": {"prompt_tokens": 5}} \xff\xfe garbage {"usage": '
        #
        assert drain(JSONValueScanner(("usage",)), body) == [("usage", {"prompt_tokens": 5})]

    def test_trailing_scalar_is_flushed_on_close(self):
        # A body that ends immediately after a number has no delimiter to terminate it,
        # so close() is what makes the final count observable.
        scanner = JSONValueScanner(("eval_count",))
        assert list(scanner.feed(b'{"eval_count": 50')) == []
        assert list(scanner.close()) == [("eval_count", 50)]

    def test_escaped_quotes_do_not_confuse_key_detection(self):
        body = b'{"content": "she said \\"usage\\": 9", "usage": {"prompt_tokens": 1}}'
        #
        assert drain(JSONValueScanner(("usage",)), body) == [("usage", {"prompt_tokens": 1})]


class TestBoundedRetention:
    """The whole point of the scanner: no body-sized buffer, ever."""

    def test_a_huge_unlisted_value_is_never_retained(self):
        # An embeddings response is megabytes of vectors followed by a tiny usage block.
        # Retention must stay flat across the vectors.
        scanner = JSONValueScanner(("usage",))
        peak = 0
        #
        scanner.feed(b'{"data": [')
        for _ in range(2000):
            scanner.feed(b'{"embedding": [' + b"0.0125," * 200 + b'0.5]},')
            peak = max(peak, scanner.retained_bytes)
        scanner.feed(b'], "usage": {"prompt_tokens": 17}}')
        #
        assert peak < 4096
        assert [key for key, _ in scanner.close()] == []

    def test_an_over_long_key_candidate_is_discarded(self):
        # Long strings are the common case in an LLM response body. None of them can be a
        # key we care about, so anything past the key-length cap is dropped as it arrives.
        scanner = JSONValueScanner(("usage",))
        scanner.feed(b'{"content": "' + b"x" * 200_000 + b'"')
        #
        assert scanner.retained_bytes <= MAX_KEY_LENGTH + 1

    def test_an_over_long_whitelisted_value_is_abandoned(self):
        # A pathological or hostile `usage` value must not become an unbounded buffer;
        # dropping it costs one unparsed reading, which is the safe failure.
        scanner = JSONValueScanner(("usage",))
        emitted = list(scanner.feed(b'{"usage": {"junk": "' + b"y" * (MAX_VALUE_BYTES + 1024)))
        #
        assert emitted == []
        assert scanner.retained_bytes <= MAX_VALUE_BYTES + MAX_KEY_LENGTH + 8

    def test_retention_collapses_to_the_overlap_after_a_value_completes(self):
        # Not zero: the scanner keeps a few trailing bytes so a key token straddling the
        # next chunk boundary is still found. The bound is the longest key, not the body.
        scanner = JSONValueScanner(("usage",))
        list(scanner.feed(b'{"usage": {"prompt_tokens": 5}}'))
        #
        assert scanner.retained_bytes < len('"usage"')

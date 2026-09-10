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

""" Every fixture in the corpus, driven end to end through the real dispatch path.

The per-dialect test files assert the reasoning specific to each provider. This file is the
uniform sweep: whatever a fixture claims in its `expected` block must come out of the real
registry, at any chunk size, and every fixture must say where its bytes came from.

That provenance rule is the point of the file. Only Azure and Bedrock credentials exist on
this project, so most payloads are either lifted verbatim from litellm's own test suite or
hand-built to a published schema. Both are legitimate; silently mixing them with captures is
not. A fixture without a `_provenance` string fails here rather than being taken on trust.
"""

import json

import pytest

from usage.sources.base import billable_input_tokens

from corpus import build_body, fixture_names, load_fixture, read

CHUNK_SIZES = [1, 7, 64, 4096]

# The full default set: a dialect with no fixture is a dialect nobody has ever exercised.
EXPECTED_DIALECTS = {
    "openai.chat", "openai.embeddings", "openai.responses", "azure.chat",
    "ai_dial.chat", "anthropic.messages", "bedrock.converse", "bedrock.invoke",
    "google.generate_content", "ollama.native",
}


def assert_matches_expected(reading, spec, context):
    """Every field the fixture names must match; the rest keeps its documented default."""
    for field, value in spec["expected"].items():
        actual = getattr(reading, field)
        assert actual == value, f"{context}: {field} was {actual!r}, expected {value!r}"


class TestCorpusIntegrity:
    """The corpus describes itself honestly, or it is not evidence of anything."""

    @pytest.mark.parametrize("name", fixture_names())
    def test_provenance_is_declared(self, name):
        spec = load_fixture(name)
        provenance = spec.get("_provenance")
        #
        assert isinstance(provenance, str), f"{name} has no _provenance string"
        assert len(provenance) > 40, f"{name} provenance is too vague to be useful"

    @pytest.mark.parametrize("name", fixture_names())
    def test_spec_is_well_formed(self, name):
        spec = load_fixture(name)
        #
        for key in ("dialect", "endpoint", "content_type", "envelope", "expected"):
            assert key in spec, f"{name} is missing {key}"
        #
        assert ("body" in spec) != ("events" in spec), f"{name} needs exactly one of body/events"
        assert "expected_billable_input" in spec, f"{name} does not state billable input"

    def test_every_default_dialect_has_a_fixture(self):
        covered = {load_fixture(name)["dialect"] for name in fixture_names()}
        #
        assert covered == EXPECTED_DIALECTS

    @pytest.mark.parametrize("name", fixture_names())
    def test_provenance_never_reaches_the_dialect(self, name):
        # The marker lives at the top level of a `body` fixture, so it would otherwise be
        # rendered into the bytes a dialect sees.
        spec = load_fixture(name)
        #
        assert b"_provenance" not in build_body(spec)


class TestCorpusReadings:
    """The whole corpus, through registry.match() and the matched dialect."""

    @pytest.mark.parametrize("name", fixture_names())
    def test_expected_reading(self, registered_dialects, name):
        spec = load_fixture(name)
        #
        reading = read(spec["dialect"], spec["endpoint"], spec["content_type"],
                       build_body(spec))
        #
        assert_matches_expected(reading, spec, name)
        assert reading.dialect == spec["dialect"]

    @pytest.mark.parametrize("name", fixture_names())
    def test_expected_billable_input(self, registered_dialects, name):
        spec = load_fixture(name)
        #
        reading = read(spec["dialect"], spec["endpoint"], spec["content_type"],
                       build_body(spec))
        #
        assert billable_input_tokens(reading) == spec["expected_billable_input"]

    @pytest.mark.parametrize("name", fixture_names())
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
    def test_chunking_changes_nothing(self, registered_dialects, name, chunk_size):
        # Chunk boundaries are whatever the network happened to do. A reading that depends
        # on them would be a billing bug that only shows up under load.
        spec = load_fixture(name)
        body = build_body(spec)
        #
        reading = read(spec["dialect"], spec["endpoint"], spec["content_type"],
                       body, chunk_size)
        #
        assert_matches_expected(reading, spec, f"{name}@{chunk_size}")

    @pytest.mark.parametrize("name", fixture_names())
    def test_truncation_never_raises(self, registered_dialects, name):
        # A dropped connection mid-response is normal. Whatever was observed by then is a
        # legitimate partial reading; an exception on the money path is not.
        spec = load_fixture(name)
        body = build_body(spec)
        #
        for fraction in (0.25, 0.5, 0.75, 0.99):
            cut = int(len(body) * fraction)
            reading = read(spec["dialect"], spec["endpoint"], spec["content_type"], body[:cut])
            assert reading.dialect == spec["dialect"]


class TestConventionSanity:
    """A cross-check on the corpus itself, independent of any single dialect."""

    @pytest.mark.parametrize("name", fixture_names())
    def test_billable_input_follows_the_stated_convention(self, registered_dialects, name):
        # Recomputed here from the reading's own fields, so a fixture that states a
        # convention its numbers contradict fails even if the dialect agrees with it.
        spec = load_fixture(name)
        reading = read(spec["dialect"], spec["endpoint"], spec["content_type"],
                       build_body(spec))
        #
        if reading.input_tokens is None:
            assert billable_input_tokens(reading) is None
            return
        #
        if reading.cache_convention == "inclusive":
            expected = max(reading.input_tokens - reading.cache_read_tokens, 0)
        else:
            expected = reading.input_tokens
        #
        assert billable_input_tokens(reading) == expected

    @pytest.mark.parametrize("name", fixture_names())
    def test_counts_are_never_negative(self, registered_dialects, name):
        spec = load_fixture(name)
        reading = read(spec["dialect"], spec["endpoint"], spec["content_type"],
                       build_body(spec))
        #
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_creation_tokens", "reasoning_tokens"):
            value = getattr(reading, field)
            assert value is None or value >= 0, f"{name}: {field} was {value!r}"


class TestFixtureRendering:
    """The builder itself, so a fixture failure is never blamed on the wrong layer."""

    @pytest.mark.parametrize("name", fixture_names())
    def test_body_renders_to_bytes(self, name):
        spec = load_fixture(name)
        #
        if spec["envelope"] in ("eventstream", "eventstream_base64"):
            pytest.importorskip("botocore")
        #
        body = build_body(spec)
        assert isinstance(body, bytes) and body

    @pytest.mark.parametrize("name", [
        name for name in fixture_names()
        if load_fixture(name)["envelope"] == "sse"
    ])
    def test_sse_fixtures_terminate_with_done(self, name):
        # Providers send [DONE] and the dialect has to tolerate it as a non-JSON payload.
        assert build_body(load_fixture(name)).endswith(b"data: [DONE]\n\n")

    @pytest.mark.parametrize("name", [
        name for name in fixture_names()
        if load_fixture(name)["envelope"] == "json"
    ])
    def test_json_fixtures_round_trip(self, name):
        spec = load_fixture(name)
        #
        decoded = json.loads(build_body(spec))
        assert "_provenance" not in decoded

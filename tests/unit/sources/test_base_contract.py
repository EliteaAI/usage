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

""" The frozen UsageReading contract and the one place cache conventions collapse """

import dataclasses

import pytest

from usage.sources import base


class TestUsageReadingDefaults:
    """Defaults are part of the contract the drainer and the models rely on."""

    def test_unobserved_counts_are_none_not_zero(self):
        # None means "the provider never told us"; 0 means "the provider said zero".
        # Collapsing the two would turn a failed parse into a free call.
        reading = base.UsageReading()
        #
        assert reading.input_tokens is None
        assert reading.output_tokens is None
        assert reading.model_name is None
        assert reading.dialect is None

    def test_additive_counts_default_to_zero(self):
        # Cache and reasoning counts are optional extras: absent means zero, and no
        # caller should have to None-guard them before adding.
        reading = base.UsageReading()
        #
        assert reading.cache_read_tokens == 0
        assert reading.cache_creation_tokens == 0
        assert reading.reasoning_tokens == 0

    def test_defaults_are_safe(self):
        # Exclusive is the conservative default: it never subtracts, so a dialect that
        # forgets to declare its convention over-bills nobody's cache discount away.
        reading = base.UsageReading()
        #
        assert reading.cache_convention == base.CACHE_EXCLUSIVE
        assert reading.token_source == base.TOKEN_SOURCE_PROVIDER

    def test_field_set_matches_the_agreed_contract(self):
        expected = {
            "model_name", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "reasoning_tokens", "cache_convention",
            "token_source", "dialect",
        }
        #
        actual = {field.name for field in dataclasses.fields(base.UsageReading)}
        assert actual == expected

    def test_token_source_values(self):
        assert base.TOKEN_SOURCE_PROVIDER == "provider"
        assert base.TOKEN_SOURCE_ESTIMATED == "estimated"
        assert base.TOKEN_SOURCE_UNPARSED == "unparsed"


class TestBillableInputTokens:
    """The single point where the two cache conventions become one number."""

    def test_inclusive_subtracts_cached(self):
        # OpenAI/Azure/Google report cached tokens as part of the prompt count, so the
        # freshly-processed remainder is what gets charged at full rate.
        reading = base.UsageReading(
            input_tokens=1024, cache_read_tokens=896,
            cache_convention=base.CACHE_INCLUSIVE,
        )
        #
        assert base.billable_input_tokens(reading) == 128

    def test_inclusive_subtracts_cache_writes_too(self):
        # LiteLLM relaying Anthropic over chat/completions folds cache writes into
        # prompt_tokens; they are priced separately, so must leave billable input.
        reading = base.UsageReading(
            input_tokens=6794, cache_creation_tokens=6761,
            cache_convention=base.CACHE_INCLUSIVE,
        )
        #
        assert base.billable_input_tokens(reading) == 33

    def test_exclusive_does_not_subtract_cached(self):
        # Anthropic/Bedrock report cached tokens on top of input. Subtracting here would
        # under-bill by the whole cache-read volume — often most of the prompt.
        reading = base.UsageReading(
            input_tokens=12, cache_read_tokens=8000,
            cache_convention=base.CACHE_EXCLUSIVE,
        )
        #
        assert base.billable_input_tokens(reading) == 12

    def test_unobserved_input_stays_unobserved(self):
        assert base.billable_input_tokens(base.UsageReading()) is None

    def test_inclusive_clamps_instead_of_going_negative(self):
        # A provider reporting more cached than prompt tokens is nonsense, but nonsense
        # must not become a negative charge.
        reading = base.UsageReading(
            input_tokens=10, cache_read_tokens=99,
            cache_convention=base.CACHE_INCLUSIVE,
        )
        #
        assert base.billable_input_tokens(reading) == 0

    def test_observed_zero_survives(self):
        reading = base.UsageReading(input_tokens=0, cache_convention=base.CACHE_INCLUSIVE)
        #
        assert base.billable_input_tokens(reading) == 0


class TestCoerceInt:
    """Provider junk must degrade to None, never raise on the money path."""

    @pytest.mark.parametrize("value,expected", [
        (5, 5),
        ("5", 5),
        (5.0, 5),
        ("5.0", None),
        (None, None),
        ("", None),
        ("abc", None),
        ({}, None),
        ([], None),
        (True, None),
        (False, None),
        (5.9, None),
        (-5, None),
        ("-5", None),
        (-0.0, 0),
    ])
    def test_coercion(self, value, expected):
        # True/False are rejected deliberately: bool is an int subclass in Python, and a
        # stray `"prompt_tokens": true` silently becoming 1 token is worse than None.
        # 5.9 and -5 are rejected too: truncating or keeping either bills a count the
        # provider never actually sent.
        assert base.coerce_int(value) == expected


class TestDigAndFirstInt:
    """Nested lookups over payloads that may be missing whole levels."""

    def test_dig_tolerates_missing_and_non_dict_levels(self):
        payload = {"usage": {"details": {"cached_tokens": 7}}}
        #
        assert base.dig(payload, "usage", "details", "cached_tokens") == 7
        assert base.dig(payload, "usage", "absent", "cached_tokens") is None
        assert base.dig(payload, "usage", "details", "cached_tokens", "deeper") is None
        assert base.dig(None, "usage") is None

    def test_first_int_picks_the_first_usable_key(self):
        # Bedrock InvokeModel is the reason this exists: the same count arrives as
        # inputTokenCount, inputTokens or input_tokens depending on the wrapped model.
        payload = {"inputTokens": None, "input_tokens": 42}
        #
        assert base.first_int(payload, "inputTokenCount", "inputTokens", "input_tokens") == 42

    def test_first_int_on_junk(self):
        assert base.first_int(None, "a") is None
        assert base.first_int({"a": "x"}, "a") is None


class TestProtocolConformance:
    """Every registered dialect satisfies the runtime-checkable protocol."""

    def test_all_default_dialects_conform(self, registered_dialects):
        for dialect_id in registered_dialects.all():
            instance = registered_dialects.get(dialect_id)
            #
            assert isinstance(instance, base.UsageDialect)
            assert instance.id == dialect_id

    def test_every_provider_is_a_real_credential_type(self, registered_dialects):
        # The narrowing key is whatever configurations reports as the credential's type, so a
        # dialect inventing its own vocabulary would simply never be narrowed to.
        for dialect_id in registered_dialects.all():
            provider = registered_dialects.get(dialect_id).provider
            #
            assert provider is None or provider in base.CREDENTIAL_PROVIDERS

    def test_every_credential_type_can_reach_a_dialect(self, registered_dialects):
        # The other direction: a credential family with no dialect means every call made with it
        # falls back to sniffing, which is the labelling hole this narrowing exists to close.
        covered = {
            registered_dialects.get(dialect_id).provider
            for dialect_id in registered_dialects.all()
        }
        #
        assert set(base.CREDENTIAL_PROVIDERS) <= covered

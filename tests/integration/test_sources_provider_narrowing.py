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

""" Every oracle-checked fixture must read the same whether or not the provider is known.

The unit tests prove narrowing picks the right label. This proves it changes no number: the
same real payloads that are cross-checked against litellm are replayed twice, once sniffed and
once narrowed by the credential family, and the two readings must agree. A provider hint that
quietly altered a token count would be a billing defect, not a labelling one.
"""

import pathlib
import sys
import types

import pytest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILDER_DIR = PLUGIN_ROOT / "tests" / "fixtures"


def _install_usage_stub():
    """Make `usage.sources.*` importable without running the plugin's __init__.py."""
    if "usage" in sys.modules:
        return
    #
    package = types.ModuleType("usage")
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["usage"] = package


_install_usage_stub()

if str(BUILDER_DIR) not in sys.path:
    sys.path.insert(0, str(BUILDER_DIR))

from corpus import (  # noqa: E402  pylint: disable=C0413
    build_body, fixture_names, load_fixture,
)
from usage.sources import registry  # noqa: E402  pylint: disable=C0413
from usage.sources.base import billable_input_tokens  # noqa: E402  pylint: disable=C0413


NUMBERS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
    "reasoning_tokens",
)


@pytest.fixture(autouse=True)
def registered_dialects():
    registry.clear()
    registry.register_defaults()
    yield registry
    registry.clear()


def _read(fixture, provider):
    dialect = registry.match(
        fixture["endpoint"], fixture["content_type"], provider=provider,
    )
    assert dialect is not None
    dialect.feed(build_body(fixture))
    #
    return dialect.result()


@pytest.mark.parametrize("name", fixture_names())
def test_narrowing_by_provider_changes_no_number(registered_dialects, name):
    fixture = load_fixture(name)
    provider = registered_dialects.get(fixture["dialect"]).provider
    #
    sniffed = _read(fixture, None)
    narrowed = _read(fixture, provider)
    #
    assert [getattr(narrowed, field) for field in NUMBERS] \
        == [getattr(sniffed, field) for field in NUMBERS]


@pytest.mark.parametrize("name", fixture_names())
def test_a_narrowed_read_keeps_the_expected_billable_input(registered_dialects, name):
    fixture = load_fixture(name)
    provider = registered_dialects.get(fixture["dialect"]).provider
    #
    reading = _read(fixture, provider)
    #
    assert billable_input_tokens(reading) == fixture["expected_billable_input"]

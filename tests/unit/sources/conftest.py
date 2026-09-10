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

""" Fixtures for the wire-dialect library tests.

These tests must pass under bare pytest as well as under tests/run_tests.py, because a
library that quietly grew a pylon dependency would still pass the harness. Importing
`usage.sources` normally executes usage/__init__.py, which imports module.py, which imports
pylon — so a stub `usage` package is installed here with __path__ pointing at the real plugin
root. Submodules then resolve for real while __init__.py never runs.
"""

import pathlib
import sys
import types

import pytest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3]
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

# Imported after the path insert above, and re-exported so tests can take either route.
from corpus import (  # noqa: E402  pylint: disable=C0413,W0611
    build_body, feed_in_chunks, fixture_names, json_body, load_fixture, ndjson_body, read,
    sse_body, strip_provenance,
)


@pytest.fixture(autouse=True)
def registered_dialects():
    """The default dialect set, per test.

    Deliberately per-test rather than session-scoped: test_no_pylon_import drops every
    usage.sources module from sys.modules, so a later test imports a *fresh* registry module
    whose _factories is empty. Re-importing and re-registering here is cheap and makes the
    order of the files irrelevant.
    """
    from usage.sources import registry  # pylint: disable=C0415

    registry.clear()
    registry.register_defaults()
    yield registry


@pytest.fixture()
def fixtures():
    """Loader handed to tests that want a fixture by name."""
    return load_fixture

"""Pytest configuration - path fixtures and directory-based auto-marking."""
import json
import pathlib

import pytest
import yaml

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def plugin_root() -> pathlib.Path:
    """Absolute path to the usage plugin root."""
    return PLUGIN_ROOT


@pytest.fixture(scope="session")
def plugin_config(plugin_root: pathlib.Path) -> dict:
    """Parsed config.yml."""
    return yaml.safe_load((plugin_root / "config.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def plugin_metadata(plugin_root: pathlib.Path) -> dict:
    """Parsed metadata.json."""
    return json.loads((plugin_root / "metadata.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def admin_schema(plugin_root: pathlib.Path) -> dict:
    """Parsed admin_schema.json."""
    return json.loads((plugin_root / "admin_schema.json").read_text(encoding="utf-8"))


@pytest.fixture()
def recording_log():
    """The stub logger installed by run_tests.py, cleared for each test."""
    from pylon.core.tools import log  # pylint: disable=C0415

    log.clear()
    yield log
    log.clear()


def pytest_collection_modifyitems(items):
    for item in items:
        if '/unit/' in str(item.fspath):
            item.add_marker(pytest.mark.unit)
        elif '/integration/' in str(item.fspath):
            item.add_marker(pytest.mark.integration)

#!/usr/bin/env python3
"""
Test runner for the usage plugin.
Installs Pylon stubs before running pytest so plugin modules can be imported.

Usage:
    python3 tests/run_tests.py -v                 # All tests
    python3 tests/run_tests.py -m unit -v         # Unit tests only
    python3 tests/run_tests.py -m integration -v  # Integration tests only
    python3 tests/run_tests.py unit/test_mode.py -v
"""
import os
import sys
import types

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(TESTS_DIR)
PLUGINS_DIR = os.path.dirname(PLUGIN_DIR)

os.chdir(TESTS_DIR)


class RecordingLog:
    """Captures log calls so tests can assert a warning was emitted, not swallowed."""

    def __init__(self):
        self.records = []

    def _record(self, level):
        def emit(*args, **kwargs):  # pylint: disable=W0613
            self.records.append((level, args[0] if args else "", args[1:]))
        return emit

    def __getattr__(self, name):
        return self._record(name)

    def messages(self, level=None):
        return [record[1] for record in self.records if level is None or record[0] == level]

    def clear(self):
        self.records.clear()


LOG = RecordingLog()


def install_pylon_stubs():
    """Install minimal Pylon stubs so plugin modules can be imported."""
    pylon = types.ModuleType('pylon')
    pylon_core = types.ModuleType('pylon.core')
    pylon_core_tools = types.ModuleType('pylon.core.tools')

    pylon_core_tools.log = LOG
    # Both decorators are identity: they only register names on a live pylon.
    pylon_core_tools.web = types.SimpleNamespace(
        rpc=lambda *a, **k: lambda f: f,
        method=lambda *a, **k: lambda f: f,
    )
    pylon_core_tools.module = types.SimpleNamespace(ModuleModel=type('ModuleModel', (), {}))

    pylon.core = pylon_core
    pylon_core.tools = pylon_core_tools

    sys.modules.setdefault('pylon', pylon)
    sys.modules.setdefault('pylon.core', pylon_core)
    sys.modules.setdefault('pylon.core.tools', pylon_core_tools)

    from sqlalchemy import MetaData  # pylint: disable=C0415
    from sqlalchemy.orm import declarative_base  # pylint: disable=C0415

    metadata = MetaData()
    base = declarative_base(metadata=metadata)

    tools = types.ModuleType('tools')
    tools.db = types.SimpleNamespace(
        Base=base,
        engine=None,
        get_session=lambda *a, **k: None,
        get_shared_metadata=lambda: metadata,
    )
    tools.config = types.SimpleNamespace(POSTGRES_SCHEMA='centry')
    tools.context = types.SimpleNamespace(rpc_manager=None)
    # Provided by `shared`, which usage depends_on, so it always resolves in a live pylon
    openapi_registry = types.SimpleNamespace(registered=[])
    openapi_registry.register_plugin = \
        lambda **kwargs: openapi_registry.registered.append(kwargs)
    tools.openapi_registry = openapi_registry
    # Default for_module() is a happy-path admin+scheduling stub, so ready()/deinit() stay quiet
    # unless a test explicitly monkeypatches `this` to inspect that behaviour.
    def _for_module(name):
        return types.SimpleNamespace(module=types.SimpleNamespace(
            register_admin_task=lambda *a, **k: None,
            unregister_admin_task=lambda *a, **k: None,
            register_managed_schedules=lambda *a, **k: None,
        ))
    #
    tools.this = types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config={}), module=None,
        for_module=_for_module,
    )
    sys.modules.setdefault('tools', tools)


if __name__ == '__main__':
    install_pylon_stubs()

    # The plugin is imported as the package `usage`, so relative imports resolve
    sys.path.insert(0, PLUGINS_DIR)

    import pytest
    sys.exit(pytest.main(['.'] + sys.argv[1:]))

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

""" Acceptance criterion: the library stays pure.

`usage/sources/` must be importable and usable with no pylon, no Flask, no SQLAlchemy and no
plugin models on the path — that is what makes it unit-testable and reusable. The single
allowed exception is one guarded `from pylon.core.tools import log` in base.py, which falls
back to stdlib logging. Rather than trusting a grep, these tests make the imports genuinely
impossible and then load every module.
"""

import importlib
import pathlib
import re
import subprocess
import sys

import pytest

SOURCES_DIR = pathlib.Path(__file__).resolve().parents[3] / "sources"

MODULES = [
    "base", "framing", "registry", "dialect", "openai_chat", "openai_embeddings",
    "openai_responses", "azure_chat", "ai_dial_chat", "anthropic_messages",
    "bedrock_converse", "bedrock_invoke", "google_generate_content", "ollama_native",
]

BANNED = ("pylon", "flask", "sqlalchemy", "tools")


class BlockingFinder:
    """A meta-path finder that makes the banned packages simply not exist."""

    def find_module(self, fullname, path=None):  # pylint: disable=W0613
        return None

    def find_spec(self, fullname, path=None, target=None):  # pylint: disable=W0613
        root = fullname.split(".", 1)[0]
        #
        if root in BANNED:
            raise ImportError(f"{root} is blocked by test_no_pylon_import")
        #
        return None


@pytest.fixture()
def without_pylon():
    """Drop the banned modules and block re-import for the duration of the test."""
    finder = BlockingFinder()
    saved = {
        name: module for name, module in sys.modules.items()
        if name.split(".", 1)[0] in BANNED
    }
    #
    for name in saved:
        del sys.modules[name]
    #
    # Force a genuine re-import of the library itself, otherwise the modules cached by
    # earlier tests would satisfy the import without ever touching the finder.
    for name in [name for name in sys.modules if name.startswith("usage.sources")]:
        del sys.modules[name]
    #
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        for name in [name for name in sys.modules if name.startswith("usage.sources")]:
            del sys.modules[name]
        sys.modules.update(saved)


class TestPurity:
    """Every module loads and works with pylon unavailable."""

    @pytest.mark.parametrize("name", MODULES)
    def test_module_imports(self, without_pylon, name):
        module = importlib.import_module(f"usage.sources.{name}")
        #
        assert module is not None

    def test_the_log_shim_falls_back_to_stdlib(self, without_pylon):
        import logging  # pylint: disable=C0415

        base = importlib.import_module("usage.sources.base")
        #
        assert isinstance(base.log, logging.Logger)
        assert base.log.name == "usage.sources"

    def test_full_extraction_works_without_pylon(self, without_pylon):
        registry = importlib.import_module("usage.sources.registry")
        registry.register_defaults()
        #
        dialect = registry.match("/v1/chat/completions", "application/json")
        dialect.feed(b'{"usage": {"prompt_tokens": 40, "completion_tokens": 12}}')
        reading = dialect.result()
        #
        assert (reading.input_tokens, reading.output_tokens) == (40, 12)


class TestSourceLevelHygiene:
    """A guard against the dependency creeping back in via a later edit."""

    def test_only_base_mentions_pylon(self):
        offenders = {}
        #
        for path in sorted(SOURCES_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            hits = re.findall(r"^\s*(?:from|import)\s+(pylon|flask|sqlalchemy|tools)\b",
                              text, re.MULTILINE)
            if hits:
                offenders[path.name] = hits
        #
        assert offenders == {"base.py": ["pylon"]}

    def test_the_pylon_import_in_base_is_guarded(self):
        text = (SOURCES_DIR / "base.py").read_text(encoding="utf-8")
        #
        assert "try:" in text
        assert "except ImportError" in text
        assert text.index("try:") < text.index("from pylon.core.tools import log")

    def test_no_plugin_model_or_sibling_plugin_imports(self):
        for path in sorted(SOURCES_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            #
            assert "from ..models" not in text, path.name
            assert "from ..tools" not in text, path.name
            assert "import models" not in text, path.name


class TestStandaloneInterpreter:
    """The strongest form of the check: a fresh process that has never seen pylon."""

    def test_library_loads_in_a_bare_interpreter(self):
        plugins_dir = SOURCES_DIR.parents[1]
        script = (
            "import sys, types\n"
            "pkg = types.ModuleType('usage')\n"
            f"pkg.__path__ = [{str(SOURCES_DIR.parent)!r}]\n"
            "sys.modules['usage'] = pkg\n"
            "from usage.sources import registry\n"
            "registry.register_defaults()\n"
            "d = registry.match('/v1/messages', 'text/event-stream')\n"
            "d.feed(b'data: {\"type\": \"message_start\", \"message\": "
            "{\"usage\": {\"input_tokens\": 10, \"output_tokens\": 1}}}\\n\\n')\n"
            "d.feed(b'data: {\"type\": \"message_delta\", "
            "\"usage\": {\"output_tokens\": 2}}\\n\\n')\n"
            "r = d.result()\n"
            "assert (r.input_tokens, r.output_tokens) == (10, 2), r\n"
            "assert 'pylon' not in sys.modules\n"
            "print('ok')\n"
        )
        #
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(plugins_dir), capture_output=True, text=True, check=False,
        )
        #
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout

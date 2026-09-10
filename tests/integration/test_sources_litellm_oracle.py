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

""" The oracle: fixtures that claim to come from litellm must still match litellm.

No provider credentials exist for Anthropic, Google or Ollama and none ever will, so those
fixtures were lifted from litellm's own test suite — litellm talks to every provider for real,
so its committed payloads are the closest thing to a capture we can have.

A citation in a `_provenance` string is only a claim, though. This test resolves each citation
against the pinned clone on disk and asserts that every token count in the fixture literally
appears in the cited lines. If someone edits a number in a fixture to make a test pass, or the
pin moves and litellm's payload changes, this fails and names the fixture.

Skipped when the pinned clone is absent, so CI without it stays green.
"""

import json
import pathlib
import re

import pytest

LITELLM_ROOT = pathlib.Path("/srv/extra_space/elitea/litellm")
WIRE_DIR = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "wire"

CITATION = re.compile(r"commit ([0-9a-f]{7,40}),?\s*file (\S+\.py) lines (\d+)-(\d+)")

# Any key whose name ends one of these carries a token count in some provider's spelling.
COUNT_KEY = re.compile(r"(tokens?|token_count|tokencount|eval_count)$", re.IGNORECASE)

pytestmark = pytest.mark.integration


def citations():
    """Every fixture that claims a litellm origin, with its citation parsed."""
    found = []
    #
    for path in sorted(WIRE_DIR.glob("*.json")):
        provenance = json.loads(path.read_text(encoding="utf-8")).get("_provenance", "")
        match = CITATION.search(provenance)
        if match:
            found.append(pytest.param(path, match, id=path.stem))
    #
    return found


def counts_in(payload):
    """Every integer that a token-count key points at, anywhere in the payload."""
    found = set()
    #
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and COUNT_KEY.search(str(key)):
                found.add(value)
            else:
                found |= counts_in(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= counts_in(item)
    #
    return found


@pytest.fixture(scope="module", autouse=True)
def pinned_clone():
    if not (LITELLM_ROOT / ".git").is_dir():
        pytest.skip(f"pinned litellm clone not present at {LITELLM_ROOT}")
    return LITELLM_ROOT


class TestCitedFixturesMatchLitellm:
    def test_there_are_citations_to_check(self):
        # A refactor that dropped the citations would otherwise make this file vacuously pass.
        assert len(citations()) >= 3

    @pytest.mark.parametrize("path, citation", citations())
    def test_the_cited_file_and_lines_exist(self, path, citation):
        cited = LITELLM_ROOT / citation.group(2)
        #
        assert cited.is_file(), f"{path.name} cites a file that is not in the clone"
        assert len(cited.read_text(encoding="utf-8").splitlines()) >= int(citation.group(4))

    @pytest.mark.parametrize("path, citation", citations())
    def test_every_token_count_appears_in_the_cited_lines(self, path, citation):
        cited = LITELLM_ROOT / citation.group(2)
        lines = cited.read_text(encoding="utf-8").splitlines()
        snippet = "\n".join(lines[int(citation.group(3)) - 1:int(citation.group(4))])
        present = {int(number) for number in re.findall(r"\d+", snippet)}
        #
        fixture = json.loads(path.read_text(encoding="utf-8"))
        # Only the wire payload is litellm's; `expected` holds our own derived reading, whose
        # numbers (Gemini's candidates+thoughts sum, for one) are deliberately not in the sample.
        wire = {key: value for key, value in fixture.items()
                if key not in ("_provenance", "expected")}
        counts = counts_in(wire)
        #
        # A fixture may add counters litellm's sample never had (a cache hit, for instance);
        # those are labelled as extensions in their own provenance and live in separate files.
        missing = {count for count in counts if count not in present}
        #
        assert not missing, (
            f"{path.name}: counts {sorted(missing)} are not in "
            f"{citation.group(2)}:{citation.group(3)}-{citation.group(4)}"
        )


class TestProvenanceDiscipline:
    def test_every_fixture_declares_provenance(self):
        undeclared = [
            path.name for path in sorted(WIRE_DIR.glob("*.json"))
            if not json.loads(path.read_text(encoding="utf-8")).get("_provenance")
        ]
        #
        assert undeclared == []

    def test_a_litellm_claim_carries_a_resolvable_citation(self):
        # "lifted from litellm" without a file and line range is not a claim anyone can check.
        vague = []
        #
        for path in sorted(WIRE_DIR.glob("*.json")):
            provenance = json.loads(path.read_text(encoding="utf-8"))["_provenance"]
            if "litellm" in provenance and not CITATION.search(provenance):
                vague.append(path.name)
        #
        # These two cite the fixture they extend or reshape, not a line range of their own.
        assert vague == ["anthropic_messages_sse_cached.json", "bedrock_invoke_eventstream.json"]

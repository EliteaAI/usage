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

""" Loading and rendering the wire-fixture corpus.

Lives here rather than in conftest.py so test modules can import it by name — pytest does not
make a conftest importable as a module. conftest.py is what puts this directory on sys.path
and installs the stub `usage` package, so importing this from a test is safe.
"""

import base64
import json
import pathlib

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "wire"


def strip_provenance(body):
    """Provenance is fixture metadata; it must never reach a dialect."""
    if isinstance(body, dict):
        return {key: value for key, value in body.items() if key != "_provenance"}
    #
    return body


def load_fixture(name):
    """One fixture spec by file stem."""
    with open(FIXTURE_DIR / f"{name}.json", "rb") as file:
        return json.loads(file.read().decode("utf-8"))


def fixture_names():
    """Every fixture stem, sorted so parametrized test ids are stable."""
    return sorted(path.stem for path in FIXTURE_DIR.glob("*.json"))


def json_body(payload):
    """A plain non-streaming JSON response body."""
    return json.dumps(strip_provenance(payload)).encode("utf-8")


def sse_body(events, done=True):
    """`data:` framed events, terminated the way every SSE provider terminates."""
    out = bytearray()
    #
    for event in events:
        out += b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
    #
    if done:
        out += b"data: [DONE]\n\n"
    #
    return bytes(out)


def ndjson_body(events):
    """One JSON object per line, Ollama style."""
    return b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events)


def build_body(spec):
    """Render a fixture spec into the exact bytes the provider would have sent."""
    envelope = spec["envelope"]
    #
    if envelope == "json":
        return json_body(spec["body"])
    #
    if envelope == "sse":
        return sse_body(spec["events"])
    #
    if envelope == "ndjson":
        return ndjson_body(spec["events"])
    #
    if envelope == "eventstream":
        import eventstream_builder  # pylint: disable=C0415
        return eventstream_builder.encode_stream(
            [(event_type, body) for event_type, body in spec["events"]]
        )
    #
    if envelope == "eventstream_base64":
        import eventstream_builder  # pylint: disable=C0415
        wrapped = []
        for event_type, body in spec["events"]:
            encoded = base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")
            wrapped.append((event_type, {"bytes": encoded}))
        return eventstream_builder.encode_stream(wrapped)
    #
    raise AssertionError(f"unknown envelope {envelope!r}")


def feed_in_chunks(dialect, body, size):
    """Drive a dialect with a fixed chunk size; size 0 means one single feed."""
    if size <= 0:
        dialect.feed(body)
    else:
        for offset in range(0, len(body), size):
            dialect.feed(body[offset:offset + size])
    #
    return dialect.result()


def read(dialect_id, endpoint, content_type, body, chunk_size=0, head_bytes=512):
    """Dispatch through the registry the way the drainer will, then read the usage."""
    from usage.sources import registry  # pylint: disable=C0415

    dialect = registry.match(endpoint, content_type, body[:head_bytes])
    assert dialect is not None, f"no dialect matched {endpoint!r}"
    assert dialect.id == dialect_id, f"expected {dialect_id!r}, matched {dialect.id!r}"
    #
    return feed_in_chunks(dialect, body, chunk_size)

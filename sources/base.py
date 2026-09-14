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

""" The contract every wire dialect implements """

import dataclasses
import typing

try:
    from pylon.core.tools import log  # pylint: disable=E0611,E0401
except ImportError:  # library-only context (tests, tooling) has no pylon on the path
    import logging
    log = logging.getLogger("usage.sources")


TOKEN_SOURCE_PROVIDER = "provider"
TOKEN_SOURCE_ESTIMATED = "estimated"
TOKEN_SOURCE_UNPARSED = "unparsed"

# The closed set of credential types configurations reports; a dialect's provider is one of these
# or None when no credential family reaches it directly.
CREDENTIAL_PROVIDERS = (
    "ai_dial", "amazon_bedrock", "azure_open_ai", "ollama", "open_ai", "vertex_ai", "wam",
)

# Whether a provider's cached-token count is part of its input count or additional to it.
CACHE_INCLUSIVE = "inclusive"
CACHE_EXCLUSIVE = "exclusive"


@dataclasses.dataclass
class UsageReading:
    """What one response reported. None means not observed; 0 means observed as zero."""

    model_name: typing.Optional[str] = None
    input_tokens: typing.Optional[int] = None
    output_tokens: typing.Optional[int] = None
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    cache_convention: str = CACHE_EXCLUSIVE
    token_source: str = TOKEN_SOURCE_PROVIDER
    dialect: typing.Optional[str] = None


@typing.runtime_checkable
class UsageDialect(typing.Protocol):
    """One wire dialect. Fed incrementally so no full body is ever buffered."""

    id: str
    # Credential type this dialect belongs to, or None when none reaches it directly.
    provider: typing.Optional[str]

    # Declared on the class: the registry probes candidates before instantiating the winner.
    @classmethod
    def matches(cls, endpoint: str, content_type: str, head: bytes) -> bool:
        ...

    def bind(self, endpoint: str, content_type: str) -> None:
        ...

    def feed(self, chunk: bytes) -> None:
        ...

    def result(self) -> UsageReading:
        ...


def billable_input_tokens(reading):
    """Full-price input tokens only, with either cache convention normalised away.

    Cached tokens are deliberately NOT folded in: providers bill them at their own rates
    (Anthropic reads ~0.1x, writes ~1.25x), so the caller must price
    `cache_read_tokens` and `cache_creation_tokens` as separate line items in both
    conventions, or cached traffic is under-billed.
    """
    if reading.input_tokens is None:
        return None
    #
    if reading.cache_convention == CACHE_INCLUSIVE:
        return max(0, reading.input_tokens - (reading.cache_read_tokens or 0))
    #
    return max(0, reading.input_tokens)


def coerce_int(value):
    """None for anything not a whole, non-negative count — truncating 5.9 or keeping -5 bills a
    number the provider never actually sent."""
    if value is None or isinstance(value, bool):
        return None
    #
    if isinstance(value, float) and not value.is_integer():
        return None
    #
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    #
    return parsed if parsed >= 0 else None


def dig(payload, *keys):
    """Nested lookup tolerating missing, None and non-dict levels."""
    node = payload
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    #
    return node


def first_int(payload, *keys):
    """First key of `keys` that yields a usable integer."""
    if not isinstance(payload, dict):
        return None
    #
    for key in keys:
        value = coerce_int(payload.get(key))
        if value is not None:
            return value
    #
    return None

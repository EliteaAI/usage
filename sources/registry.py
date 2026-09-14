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

""" Dialect registry — core code talks to this, never to a concrete dialect

Mirrors costs/sources/registry.py, with one deliberate difference: a dialect is a
per-response state machine, so factories are registered and match() hands back a fresh
instance rather than a shared singleton.
"""

from .base import log

_factories = {}
_order = []
_by_provider = {}


def register(factory) -> None:
    """`factory` is any zero-arg callable carrying an `id` — normally the dialect class."""
    dialect_id = factory.id
    #
    if dialect_id not in _factories:
        _order.append(dialect_id)
    #
    _factories[dialect_id] = factory
    #
    provider = getattr(factory, "provider", None)
    if provider:
        _by_provider.setdefault(provider, [])
        if dialect_id not in _by_provider[provider]:
            _by_provider[provider].append(dialect_id)
    #
    log.info("usage.sources: registered %r (provider=%r)", dialect_id, provider)


def get(dialect_id, endpoint="", content_type=""):
    """A fresh, bound instance of one dialect by id, or None.

    Pass the request context: a dialect reading AWS binary frames installs its framer in
    bind(), so an instance built without a content type would read an event stream as text
    and report nothing.
    """
    factory = _factories.get(dialect_id)
    if factory is None:
        return None
    #
    return _bind(factory, endpoint or "", content_type or "")


def all() -> dict:  # pylint: disable=W0622
    return dict(_factories)


def clear() -> None:
    """Test seam — the live process registers once at import and never unregisters."""
    _factories.clear()
    del _order[:]
    _by_provider.clear()


def match(endpoint, content_type, head=b"", provider=None):
    """The dialect that owns this response, or None (caller records it as unparsed).

    `provider` is the credential family actually in use, so it is reliable where a URL is not:
    a DIAL credential on an api_base with no `dial` marker is unrecognisable by sniffing alone.
    It narrows the candidates; the endpoint and body still decide the shape.
    """
    if not _order:
        log.warning("usage.sources: registry is empty — register_defaults() was never called")
        return None
    #
    endpoint = endpoint or ""
    content_type = content_type or ""
    #
    candidates = _candidates(provider)
    #
    # Shape-only while narrowed: the provider is already established, so a dialect must not be
    # rejected for lacking the URL marker that would have identified its provider by sniffing.
    dialect = _probe(
        candidates, endpoint, content_type, head, shape_only=candidates is not _order,
    )
    if dialect is not None:
        return dialect
    #
    # A provider has no dialect for every shape it can serve — DIAL on /v1/embeddings is read by
    # openai.embeddings — so narrowing must never be the reason a readable body goes unparsed.
    if candidates is not _order:
        log.debug(
            "usage.sources: provider %r owns no dialect for endpoint=%r, sniffing all",
            provider, endpoint,
        )
        dialect = _probe(_order, endpoint, content_type, head)
        if dialect is not None:
            return dialect
    #
    log.warning(
        "usage.sources: no dialect owns endpoint=%r content_type=%r provider=%r — usage unparsed",
        endpoint, content_type, provider,
    )
    return None


def _candidates(provider):
    """The dialect ids to probe: the provider's own, or every registered one."""
    if not provider:
        return _order
    #
    narrowed = _by_provider.get(provider)
    if not narrowed:
        log.warning(
            "usage.sources: provider %r has no registered dialect, sniffing instead", provider,
        )
        return _order
    #
    return narrowed


def _probe(candidates, endpoint, content_type, head, shape_only=False):
    """Stage 1 asks on endpoint and content-type alone; stage 2 re-asks with head bytes for
    bodies whose endpoint was not distinctive enough."""
    probes = [b""] if not head else [b"", head]
    #
    for probe in probes:
        for dialect_id in candidates:
            factory = _factories[dialect_id]
            predicate = factory.matches_shape if shape_only else factory.matches
            try:
                # Probed on the class: matches() is a predicate, so instantiating every
                # candidate just to ask would allocate a scanner per dialect per response.
                if predicate(endpoint, content_type, probe):
                    return _bind(factory, endpoint, content_type)
            except Exception:  # pylint: disable=W0703
                log.warning(
                    "usage.sources: %r matches() raised for endpoint=%r content_type=%r",
                    dialect_id, endpoint, content_type, exc_info=True,
                )
    #
    return None


def _bind(factory, endpoint, content_type):
    """A fresh instance, given the request context it needs before any bytes arrive."""
    dialect = factory()
    dialect.bind(endpoint, content_type)
    return dialect


def register_defaults() -> None:
    """Register the built-in dialects. Order matters: the specific before the generic."""
    from .azure_chat import AzureChatDialect
    from .ai_dial_chat import AiDialChatDialect
    from .openai_responses import OpenAIResponsesDialect
    from .openai_embeddings import OpenAIEmbeddingsDialect
    from .openai_chat import OpenAIChatDialect
    from .anthropic_messages import AnthropicMessagesDialect
    from .bedrock_converse import BedrockConverseDialect
    from .bedrock_invoke import BedrockInvokeDialect
    from .google_generate_content import GoogleGenerateContentDialect
    from .ollama_native import OllamaNativeDialect
    from .wam_chat import WamChatDialect

    # Ollama first: its paths are exact, and /api/embeddings would otherwise be taken by the
    # openai.embeddings suffix match.
    for dialect_class in (
            OllamaNativeDialect,
            AzureChatDialect,
            AiDialChatDialect,
            OpenAIResponsesDialect,
            OpenAIEmbeddingsDialect,
            OpenAIChatDialect,
            AnthropicMessagesDialect,
            BedrockConverseDialect,
            BedrockInvokeDialect,
            GoogleGenerateContentDialect,
            WamChatDialect,
    ):
        register(dialect_class)

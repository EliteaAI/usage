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


def register(factory) -> None:
    """`factory` is any zero-arg callable carrying an `id` — normally the dialect class."""
    dialect_id = factory.id
    #
    if dialect_id not in _factories:
        _order.append(dialect_id)
    #
    _factories[dialect_id] = factory
    log.info("usage.sources: registered %r", dialect_id)


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


def match(endpoint, content_type, head=b"", dialect_hint=None):
    """The dialect that owns this response, or None (caller records it as unparsed).

    `dialect_hint` is authoritative when it names a registered dialect: the caller routed the
    request and knows the provider, which beats sniffing a body. Stage 1 then asks on endpoint
    and content-type alone; stage 2 re-asks with head bytes for bodies whose endpoint was not
    distinctive enough.
    """
    if not _order:
        log.warning("usage.sources: registry is empty — register_defaults() was never called")
        return None
    #
    endpoint = endpoint or ""
    content_type = content_type or ""
    #
    hinted = _hinted(dialect_hint, endpoint, content_type)
    if hinted is not None:
        return _bind(hinted, endpoint, content_type)
    #
    probes = [b""] if not head else [b"", head]
    #
    for probe in probes:
        for dialect_id in _order:
            factory = _factories[dialect_id]
            try:
                # Probed on the class: matches() is a predicate, so instantiating every
                # candidate just to ask would allocate a scanner per dialect per response.
                if factory.matches(endpoint, content_type, probe):
                    return _bind(factory, endpoint, content_type)
            except Exception:  # pylint: disable=W0703
                log.warning(
                    "usage.sources: %r matches() raised for endpoint=%r content_type=%r",
                    dialect_id, endpoint, content_type, exc_info=True,
                )
    #
    log.warning(
        "usage.sources: no dialect owns endpoint=%r content_type=%r — usage unparsed",
        endpoint, content_type,
    )
    return None


def _hinted(dialect_hint, endpoint, content_type):
    """The hinted factory, or None to fall back to sniffing. The hint is authoritative even
    when it disagrees with sniffing — but a disagreement is worth a grep-able line."""
    if not dialect_hint:
        return None
    #
    factory = _factories.get(dialect_hint)
    if factory is None:
        log.warning(
            "usage.sources: dialect hint %r is not registered, sniffing instead", dialect_hint,
        )
        return None
    #
    try:
        if not factory.matches(endpoint, content_type, b""):
            log.debug(
                "usage.sources: dialect hint %r disagrees with sniffing for endpoint=%r",
                dialect_hint, endpoint,
            )
    except Exception:  # pylint: disable=W0703
        pass
    #
    return factory


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
    ):
        register(dialect_class)

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


def get(dialect_id):
    """A fresh instance of one dialect by id, or None."""
    factory = _factories.get(dialect_id)
    return factory() if factory is not None else None


def all() -> dict:  # pylint: disable=W0622
    return dict(_factories)


def clear() -> None:
    """Test seam — the live process registers once at import and never unregisters."""
    _factories.clear()
    del _order[:]


def match(endpoint, content_type, head=b""):
    """The dialect that owns this response, or None (caller records it as unparsed).

    Stage 1 asks on endpoint and content-type alone; stage 2 re-asks with head bytes so a
    dialect can probe the body shape when the endpoint was not distinctive enough.
    """
    endpoint = endpoint or ""
    content_type = content_type or ""
    #
    probes = [b""] if not head else [b"", head]
    #
    for probe in probes:
        for dialect_id in _order:
            candidate = _factories[dialect_id]()
            try:
                if candidate.matches(endpoint, content_type, probe):
                    return candidate
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

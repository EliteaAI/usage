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

""" EPAM WAM — a cookie-authenticated facade over Azure OpenAI, so an openai.chat body """

from .dialect import path_of
from .openai_chat import CHAT_PATHS, OpenAIChatDialect


class WamChatDialect(OpenAIChatDialect):
    """Reached by credential family alone: the interface meters the client-facing OpenAI path
    and rewrites only the upstream url, so WAM traffic carries no marker of its own."""

    id = "wam.chat"
    provider = "wam"

    @classmethod
    def matches(cls, endpoint, content_type, head):
        return False

    @classmethod
    def matches_shape(cls, endpoint, content_type, head):
        # Path only, unlike the inherited body fallback: an embeddings response also reports
        # prompt_tokens, and taking /v1/embeddings here would lose its output side entirely.
        return path_of(endpoint).rstrip("/").endswith(CHAT_PATHS)

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

""" EPAM AI DIAL — an Azure-compatible facade, so an openai.chat body """

from .dialect import path_of, query_of
from .openai_chat import CHAT_PATHS, OpenAIChatDialect
from .azure_chat import DEPLOYMENT_MARKER


class AiDialChatDialect(OpenAIChatDialect):
    """Deployment path without Azure's mandatory `api-version`, or an explicit DIAL host."""

    id = "ai_dial.chat"

    @classmethod
    def matches(cls, endpoint, content_type, head):
        path = path_of(endpoint).rstrip("/")
        #
        if not path.endswith(CHAT_PATHS):
            return False
        #
        if "dial" in path.lower():
            return True
        #
        return DEPLOYMENT_MARKER in path and "api-version" not in query_of(endpoint)

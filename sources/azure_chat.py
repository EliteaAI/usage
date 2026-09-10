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

""" Azure OpenAI — same body as openai.chat, reached over a deployment path """

from .dialect import path_of, query_of
from .openai_chat import CHAT_PATHS, OpenAIChatDialect

DEPLOYMENT_MARKER = "/openai/deployments/"


class AzureChatDialect(OpenAIChatDialect):
    """Only the label differs from openai.chat; Azure's own paths carry `api-version`."""

    id = "azure.chat"

    def matches(self, endpoint, content_type, head):
        path = path_of(endpoint).rstrip("/")
        #
        if DEPLOYMENT_MARKER not in path or not path.endswith(CHAT_PATHS):
            return False
        #
        return "api-version" in query_of(endpoint)

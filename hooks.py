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

""" Interface hooks — signatures frozen here, bodies land with the drainer and the gate

Resolved lazily at call time (tools.usage_hooks), so neither side needs init_after.
"""

import dataclasses
import typing


@dataclasses.dataclass
class UsageContext:
    """Handle an interface carries from begin_llm_call to meter_llm_response."""

    project_id: int = None
    user_id: int = None
    model_name: str = None
    endpoint: str = None
    denied: bool = False
    response: typing.Any = None


def begin_llm_call(project_id, user_id, model_name, endpoint, headers):  # pylint: disable=W0613
    """None when metering is inactive; a UsageContext otherwise.

    model_name is RAW/pre-mapping: LiteLLM rewrites it, the costs catalog uses the raw name.
    """
    return None


def meter_llm_response(ctx, response, iterator):  # pylint: disable=W0613
    """Returns the iterator to serve. Identity while there is nothing to meter."""
    return iterator

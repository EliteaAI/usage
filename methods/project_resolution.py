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

""" Project resolution helper for NEW runtime interfaces

litellm keeps its own working copy; nothing is moved, this just saves reinventing the ladder.
"""

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import context  # pylint: disable=E0401

PROJECT_HEADERS = ("X-Project-Id", "OpenAI-Organization")


def read_header_project_id(headers):
    """Requested project id from either header, or None when absent/unparseable."""
    for header in PROJECT_HEADERS:
        raw = (headers or {}).get(header)
        #
        if raw is None:
            continue
        #
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.warning("usage: header %s is not an integer project id", header)
    #
    return None


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_resolve_project_id(self, user_id, user_name, headers, project_user_prefix=None):
        """Project a call is billed to: header (membership-checked), then service-user name,
        then the caller's personal project. None when nothing resolves."""
        candidate = read_header_project_id(headers)
        #
        # Membership-checked so a token holder cannot spend on a project they do not belong to
        if candidate is not None and self.usage_user_in_project(candidate, user_id):
            return candidate
        #
        if project_user_prefix and (user_name or "").startswith(project_user_prefix):
            try:
                return int(user_name.split(":")[-2])
            except (IndexError, ValueError):
                log.warning("usage: cannot read project id out of service user name")
        #
        try:
            return context.rpc_manager.timeout(30).projects_get_personal_project_id(user_id)
        except:  # pylint: disable=W0702
            log.exception("usage: failed to resolve personal project for user %s", user_id)
        #
        return None

    @web.method()
    def usage_user_in_project(self, project_id, user_id):
        """Membership check that fails closed."""
        try:
            return bool(
                context.rpc_manager.timeout(30).admin_check_user_in_project(project_id, user_id)
            )
        except:  # pylint: disable=W0702
            log.exception("usage: failed to check membership of user %s in %s", user_id, project_id)
        #
        return False

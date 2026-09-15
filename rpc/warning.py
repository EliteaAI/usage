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

""" Budget warning RPC """

from pylon.core.tools import web  # pylint: disable=E0611,E0401


class RPC:  # pylint: disable=E1101,R0903,W0201
    """ RPC Resource """

    @web.rpc("usage_get_budget_warning_state", "usage_budget_warning_state_now")
    def usage_get_budget_warning_state_rpc(self, project_id, user_id=None, **kwargs):  # pylint: disable=W0613
        """Whether a budget is nearing its limit for this user, and which scope."""
        return self.usage_get_budget_warning_state(project_id, user_id)

    @web.rpc("usage_get_warning_threshold", "usage_warning_threshold_now")
    def usage_get_warning_threshold_rpc(self, scope, **kwargs):  # pylint: disable=W0613
        """Configured warning percentage for a budget scope."""
        return self.usage_get_warning_threshold(scope)

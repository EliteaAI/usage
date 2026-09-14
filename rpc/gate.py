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

""" Gate RPC """

from pylon.core.tools import web  # pylint: disable=E0611,E0401


class RPC:  # pylint: disable=E1101,R0903,W0201
    """ RPC Resource """

    @web.rpc("usage_gate_check", "usage_gate_check_now")
    def usage_gate_check_rpc(self, project_id, user_id=None, **kwargs):  # pylint: disable=W0613
        """Read-only budget-door answer for callers off the inference plane."""
        if not self.usage_is_enforcing():
            return {"closed": False, "scope": None, "healthy": True}
        #
        return self.usage_gate_check(project_id, user_id)

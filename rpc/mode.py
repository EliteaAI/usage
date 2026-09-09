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

""" Mode RPC """

from pylon.core.tools import web  # pylint: disable=E0611,E0401


class RPC:  # pylint: disable=E1101,R0903,W0201
    """ RPC Resource """

    @web.rpc("usage_mode", "usage_mode")
    def usage_mode_rpc(self, **kwargs):  # pylint: disable=W0613
        """Current usage mode, so the UI can hide the feature when it is off."""
        return self.usage_get_mode()

    @web.rpc("usage_spend_source", "usage_spend_source")
    def usage_spend_source_rpc(self, **kwargs):  # pylint: disable=W0613
        """Resolved backend the spend facade reads from."""
        return self.usage_get_spend_source()

    @web.rpc("usage_ensure_partitions", "usage_ensure_partitions_now")
    def usage_ensure_partitions_rpc(self, **kwargs):  # pylint: disable=W0613
        """Create the current and upcoming usage_event partitions. Called by the daily cron."""
        return self.usage_ensure_partitions()

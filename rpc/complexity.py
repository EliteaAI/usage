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

""" Complexity RPC — cron entry points """

from pylon.core.tools import web  # pylint: disable=E0611,E0401


class RPC:  # pylint: disable=E1101,R0903,W0201
    """ RPC Resource """

    @web.rpc("usage_complexity_score_runs", "usage_complexity_score_runs_now")
    def usage_complexity_score_runs_rpc(self, **kwargs):  # pylint: disable=W0613
        """Hourly cron: score runs that went quiet. Returns at once, the work runs in a thread."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        return self.usage_complexity_spawn("usage-complexity-score", self.usage_complexity_score)

    @web.rpc("usage_complexity_conversations_by_label", "usage_complexity_conversations_by_label")
    def usage_complexity_conversations_by_label_rpc(self, entity_project_id, entity_id, dt_from, dt_to, labels,
                                                    **kwargs):  # pylint: disable=W0613
        """Read-only (#7682 BE-2): the conversation ids of an entity's runs with one of the complexity labels."""
        if not self.usage_complexity_is_enabled():
            return {"conversation_ids": [], "truncated": False, "skipped": "disabled"}
        return self.usage_complexity_labelled_conversation_ids(
            entity_project_id, entity_id, dt_from, dt_to, labels,
        )

    @web.rpc("usage_complexity_nightly", "usage_complexity_nightly_now")
    def usage_complexity_nightly_rpc(self, **kwargs):  # pylint: disable=W0613
        """Nightly cron: catch-up scoring, calibration, retention."""
        if not self.usage_complexity_is_enabled():
            return {"skipped": "disabled"}
        return self.usage_complexity_spawn("usage-complexity-nightly", self.usage_complexity_nightly)

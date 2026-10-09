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

""" Spend RPC facade — named RPCs returning already-aggregated, server-side-paged rows

Not a generic aggregate(dims, metrics, filters) DSL — arbitrary dims cannot be index-proven.

Thin delegation only: the aggregation SQL lives in methods/spend.py. The zero shapes below are
the degraded answer for a failed read, and stay here because they are the page's contract.
"""

import datetime

from pylon.core.tools import web  # pylint: disable=E0611,E0401


def current_period():
    """YYYYMM of the current UTC month."""
    return f"{datetime.datetime.now(datetime.timezone.utc):%Y%m}"


def empty_spend(tag=""):
    """The zero spend shape the Usage page reads while no data is available."""
    return {
        "tag": tag,
        "period": current_period(),
        "spend": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "available": False,
    }


def empty_usage_detail(tag=""):
    """The zero usage-detail shape the Usage page reads while no data is available."""
    return {
        "tag": tag,
        "period": current_period(),
        "models": [],
        "daily": [],
        "spend": 0.0,
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "api_requests": 0,
        "available": False,
    }


class RPC:  # pylint: disable=E1101,R0903,W0201
    """ RPC Resource """

    # The Python attribute names carry an _rpc suffix on purpose: binding this class after
    # spend.Method onto the same Module would otherwise overwrite the method; the methods they call
    # are named usage_read_* for the same reason — pylon rejects a registry name claimed twice.

    @web.rpc("usage_get_project_spend", "usage_get_project_spend")
    def usage_get_project_spend_rpc(self, project_id, **kwargs):
        """Current-month spend for a project."""
        return self.usage_read_project_spend(project_id=project_id, **kwargs)

    @web.rpc("usage_get_projects_spend", "usage_get_projects_spend")
    def usage_get_projects_spend_rpc(self, project_ids, **kwargs):
        """Current-month spend for many projects, keyed by project id."""
        return self.usage_read_projects_spend(project_ids=project_ids, **kwargs)

    @web.rpc("usage_get_user_spend", "usage_get_user_spend")
    def usage_get_user_spend_rpc(self, project_id, user_id, **kwargs):
        """Current-month spend for one member of a project."""
        return self.usage_read_user_spend(project_id=project_id, user_id=user_id, **kwargs)

    @web.rpc("usage_get_users_spend", "usage_get_users_spend")
    def usage_get_users_spend_rpc(self, project_id, user_ids, **kwargs):
        """Current-month spend for many members of a project, keyed by user id."""
        return self.usage_read_users_spend(project_id=project_id, user_ids=user_ids, **kwargs)

    @web.rpc("usage_list_member_spend", "usage_list_member_spend")
    def usage_list_member_spend_rpc(self, project_id, period=None, **kwargs):
        """Members with recorded spend, plus the project total.

        None means unreachable — callers already have a degraded branch for it.
        """
        return self.usage_read_member_spend_listing(project_id=project_id, period=period, **kwargs)

    @web.rpc("usage_get_project_usage_detail", "usage_get_project_usage_detail")
    def usage_get_project_usage_detail_rpc(self, project_id, **kwargs):
        """Per-model and per-day current-month usage for a project."""
        return self.usage_read_project_usage_detail(project_id=project_id, **kwargs)

    @web.rpc("usage_get_user_usage_detail", "usage_get_user_usage_detail")
    def usage_get_user_usage_detail_rpc(self, project_id, user_id, **kwargs):
        """Per-model and per-day current-month usage for one member of a project."""
        return self.usage_read_user_usage_detail(
            project_id=project_id, user_id=user_id, **kwargs,
        )

    @web.rpc("usage_conversation_totals", "usage_conversation_totals")
    def usage_conversation_totals_rpc(self, project_id, conversation_ids, **kwargs):
        return self.usage_read_conversation_totals(
            project_id=project_id, conversation_ids=conversation_ids, **kwargs,
        )

    @web.rpc("usage_root_entity_models", "usage_root_entity_models")
    def usage_root_entity_models_rpc(
            self, project_id, conversation_ids, root_entity_type, root_entity_id, **kwargs,
    ):
        return self.usage_read_root_entity_models(
            project_id=project_id, conversation_ids=conversation_ids,
            root_entity_type=root_entity_type, root_entity_id=root_entity_id, **kwargs,
        )

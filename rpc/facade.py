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

Every reader here answers with the zero shape until #6574 wires it to usage_event/usage_counter.
The shapes are the contract the Usage page already consumes, so they are kept rather than removed.
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

    @web.rpc("usage_get_project_spend", "usage_get_project_spend")
    def usage_get_project_spend(self, project_id, **kwargs):  # pylint: disable=W0613
        """Current-month spend for a project."""
        return empty_spend()

    @web.rpc("usage_get_projects_spend", "usage_get_projects_spend")
    def usage_get_projects_spend(self, project_ids, **kwargs):  # pylint: disable=W0613
        """Current-month spend for many projects, keyed by project id."""
        return {project_id: 0.0 for project_id in project_ids}

    @web.rpc("usage_get_user_spend", "usage_get_user_spend")
    def usage_get_user_spend(self, project_id, user_id, **kwargs):  # pylint: disable=W0613
        """Current-month spend for one member of a project."""
        return empty_spend()

    @web.rpc("usage_get_users_spend", "usage_get_users_spend")
    def usage_get_users_spend(self, project_id, user_ids, **kwargs):  # pylint: disable=W0613
        """Current-month spend for many members of a project, keyed by user id."""
        return {user_id: 0.0 for user_id in user_ids}

    @web.rpc("usage_list_member_spend", "usage_list_member_spend")
    def usage_list_member_spend(self, project_id, period=None, **kwargs):  # pylint: disable=W0613
        """Members with recorded spend, plus the project total.

        None means unreachable — callers already have a degraded branch for it.
        """
        return None

    @web.rpc("usage_get_project_usage_detail", "usage_get_project_usage_detail")
    def usage_get_project_usage_detail(self, project_id, **kwargs):  # pylint: disable=W0613
        """Per-model and per-day current-month usage for a project."""
        return empty_usage_detail()

    @web.rpc("usage_get_user_usage_detail", "usage_get_user_usage_detail")
    def usage_get_user_usage_detail(self, project_id, user_id, **kwargs):  # pylint: disable=W0613
        """Per-model and per-day current-month usage for one member of a project."""
        return empty_usage_detail()

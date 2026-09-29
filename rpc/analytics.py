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

""" Analytics aggregation RPC over usage_event

A named RPC per report shape, so a plugin that needs an AI-adoption number gets one here
rather than an ORM model of usage_event of its own (#6574).
"""

from pylon.core.tools import web  # pylint: disable=E0611,E0401

from ..methods import _analytics as an


class RPC:  # pylint: disable=E1101,R0903,W0201
    """ RPC Resource """

    @web.rpc("usage_ai_active_users_trend", "usage_ai_active_users_trend")
    def usage_ai_active_users_trend(  # pylint: disable=R0913,R0917
            self, project_id, date_from=None, date_to=None,
            granularity="day", roles=None, **kwargs,
    ):  # pylint: disable=W0613
        """Distinct AI-active users per calendar day/week/month bucket for a project.

        Same implementation as the REST endpoint, so elitea_core can put this number in the
        same bucket list as its own generic active-users count without the two drifting.
        """
        return an.ai_active_users_trend(project_id, date_from, date_to, granularity, roles)

    @web.rpc("usage_event_type_health", "usage_event_type_health")
    def usage_event_type_health(  # pylint: disable=R0913,R0917
            self, project_id, date_from=None, date_to=None, run_id=None, eval_run_id=None, **kwargs,
    ):  # pylint: disable=W0613
        """llm/tool health rows for elitea_core's Health tab, so it matches Overview's counts.

        run_id/eval_run_id scope it to one run (see _analytics.parse_run_scope); without explicit
        bounds the run is the window. Raises ValueError/LookupError for a malformed/unknown run.
        """
        run_scope = an.parse_run_scope(project_id, {"run_id": run_id, "eval_run_id": eval_run_id})
        if run_scope is not None and date_from is None and date_to is None:
            date_from, date_to = run_scope.dt_from, run_scope.dt_to
        return an.event_type_health(project_id, date_from, date_to, run_scope=run_scope)

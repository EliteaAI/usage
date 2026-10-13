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

""" Break-glass admin tasks: usage_event partitions and schema """

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_ensure_partitions_now_task(self, *args, **kwargs):  # pylint: disable=W0613
        """Create the current month's usage_event partition plus the configured lookahead now."""
        created = self.usage_ensure_partitions()
        log.info("usage: admin task ensured %s usage_event partition(s)", created)
        #
        return created

    @web.method()
    def usage_ensure_role_snapshot_column_task(self, *args, **kwargs):  # pylint: disable=W0613
        """Add usage_event.role_snapshot if missing, then stamp current project roles onto rows
        written before it existed (#6796). Safe to rerun: only NULL rows are touched. Rows of
        users already removed from their project stay NULL -- their roles are gone.
        """
        self.usage_ensure_role_snapshot_column()
        log.info("usage: admin task ensured usage_event.role_snapshot")
        #
        return self.usage_backfill_role_snapshot()

    @web.method()
    def usage_ensure_root_entity_project_column_task(self, *args, **kwargs):  # pylint: disable=W0613
        """Add usage_event.root_entity_project_id if missing (#6902). Safe to rerun. No backfill:
        rows written before it existed read as their own project_id.
        """
        self.usage_ensure_root_entity_project_column()
        log.info("usage: admin task ensured usage_event.root_entity_project_id")

    @web.method()
    def usage_ensure_budget_exempt_column_task(self, *args, **kwargs):  # pylint: disable=W0613
        """Add usage_event.budget_exempt if missing. Safe to rerun. No backfill: old rows count."""
        self.usage_ensure_budget_exempt_column()
        log.info("usage: admin task ensured usage_event.budget_exempt")

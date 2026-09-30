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

""" usage_event.role_snapshot column (#6796)

create_all provisions a new database from the model, but never alters a table that already
exists, so a deployed usage_event needs this ALTER run once by hand -- the same gap the
now-removed cost-split migration (docs/6574-cost-split-migration.sql) used to cover. Unlike
that one, this column needs no backfill: it is nullable, and a NULL row simply keeps today's
live-join behaviour (see methods/_analytics.py role_filter_condition), so there is nothing to
freeze for rows written before the column existed.
"""

from sqlalchemy import text

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db, config as c  # pylint: disable=E0401

ROLE_SNAPSHOT_COLUMN = "role_snapshot"


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_ensure_role_snapshot_column(self):
        """Add usage_event.role_snapshot if a deployed table predates it; a no-op once it
        exists. ADD COLUMN IF NOT EXISTS on the partitioned parent cascades to every
        partition, existing and future, in the one statement.
        """
        schema = c.POSTGRES_SCHEMA
        #
        with db.engine.connect() as connection:
            connection.execute(text(
                f"ALTER TABLE {schema}.usage_event "
                f"ADD COLUMN IF NOT EXISTS {ROLE_SNAPSHOT_COLUMN} TEXT[]"
            ))
            connection.commit()
        #
        log.info("usage: ensured %s.usage_event.%s", schema, ROLE_SNAPSHOT_COLUMN)

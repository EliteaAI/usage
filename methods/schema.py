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
now-removed cost-split migration (docs/6574-cost-split-migration.sql) used to cover.

A NULL row falls back to the live project_user_role join (see methods/_analytics.py
role_filter_condition), so a row written before the column existed still vanishes the moment
its actor is removed. usage_backfill_role_snapshot freezes those rows with the actor's roles as
of the backfill. A user already removed by then has no roles left to read, so their old rows
stay NULL: that history is not recoverable from project_user_role.
"""

from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db, config as c  # pylint: disable=E0401

from . import _analytics as an
from ..models.usage_event import SKILL_INDEX_NAME, SKILL_INDEX_PREDICATE

ROLE_SNAPSHOT_COLUMN = "role_snapshot"
ROOT_ENTITY_PROJECT_COLUMN = "root_entity_project_id"
BUDGET_EXEMPT_COLUMN = "budget_exempt"
SKILL_INDEX_COLUMNS = "(project_id, entity_id, ts)"


def parent_index_statement(schema):
    """ Helper """
    return (
        f"CREATE INDEX IF NOT EXISTS {SKILL_INDEX_NAME} ON ONLY {schema}.usage_event "
        f"{SKILL_INDEX_COLUMNS} WHERE {SKILL_INDEX_PREDICATE}"
    )


def partition_index_name(partition):
    """ Helper """
    return f"{partition}_skill_entity_ts"


def partition_index_statement(schema, partition):
    """ Helper """
    return (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {partition_index_name(partition)} "
        f"ON {schema}.{partition} {SKILL_INDEX_COLUMNS} WHERE {SKILL_INDEX_PREDICATE}"
    )


def attach_statement(schema, partition):
    """ Helper """
    return f"ALTER INDEX {schema}.{SKILL_INDEX_NAME} ATTACH PARTITION {schema}.{partition_index_name(partition)}"


_PARTITIONS_SQL = text(
    "SELECT child.relname FROM pg_inherits i "
    "JOIN pg_class child ON child.oid = i.inhrelid "
    "JOIN pg_class parent ON parent.oid = i.inhparent "
    "JOIN pg_namespace n ON n.oid = parent.relnamespace "
    "WHERE n.nspname = :schema AND parent.relname = 'usage_event' "
    "ORDER BY child.relname"
)

_ATTACHED_SQL = text(
    "SELECT 1 FROM pg_inherits i "
    "JOIN pg_class parent_index ON parent_index.oid = i.inhparent "
    "JOIN pg_namespace n ON n.oid = parent_index.relnamespace "
    "JOIN pg_index x ON x.indexrelid = i.inhrelid "
    "JOIN pg_class part ON part.oid = x.indrelid "
    "WHERE n.nspname = :schema AND parent_index.relname = :index AND part.relname = :partition"
)

# A failed CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would skip forever
_INVALID_SQL = text(
    "SELECT 1 FROM pg_class ci JOIN pg_namespace n ON n.oid = ci.relnamespace "
    "JOIN pg_index x ON x.indexrelid = ci.oid "
    "WHERE n.nspname = :schema AND ci.relname = :index AND NOT x.indisvalid"
)


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

    @web.method()
    def usage_ensure_root_entity_project_column(self):
        """Add usage_event.root_entity_project_id (#6902) if a deployed table predates it; a
        no-op once it exists. Cascades to every partition like role_snapshot. No backfill: NULL
        reads as the row's own project_id.
        """
        schema = c.POSTGRES_SCHEMA
        #
        with db.engine.connect() as connection:
            connection.execute(text(
                f"ALTER TABLE {schema}.usage_event "
                f"ADD COLUMN IF NOT EXISTS {ROOT_ENTITY_PROJECT_COLUMN} BIGINT"
            ))
            connection.commit()
        #
        log.info("usage: ensured %s.usage_event.%s", schema, ROOT_ENTITY_PROJECT_COLUMN)

    @web.method()
    def usage_ensure_budget_exempt_column(self):
        """Add usage_event.budget_exempt if a deployed table predates it; a no-op once it
        exists. Cascades to every partition like role_snapshot. No backfill: NULL is counted.
        """
        schema = c.POSTGRES_SCHEMA
        #
        with db.engine.connect() as connection:
            connection.execute(text(
                f"ALTER TABLE {schema}.usage_event "
                f"ADD COLUMN IF NOT EXISTS {BUDGET_EXEMPT_COLUMN} BOOLEAN"
            ))
            connection.commit()
        #
        log.info("usage: ensured %s.usage_event.%s", schema, BUDGET_EXEMPT_COLUMN)

    @web.method()
    def usage_backfill_role_snapshot(self):
        """Stamp current project roles onto usage_event rows whose role_snapshot is NULL.

        Idempotent: only NULL rows are touched, so a rerun picks up where a failed one stopped.
        One auth lookup and one transaction per project; a project whose lookup fails is
        skipped and left for the next run rather than stamped as "no roles".
        """
        schema = c.POSTGRES_SCHEMA
        #
        with db.engine.connect() as connection:
            pairs = connection.execute(text(
                f"SELECT DISTINCT project_id, user_id FROM {schema}.usage_event "
                f"WHERE {ROLE_SNAPSHOT_COLUMN} IS NULL"
            )).all()
        #
        users_by_project = {}
        #
        for project_id, user_id in pairs:
            users_by_project.setdefault(int(project_id), set()).add(int(user_id))
        #
        update = text(
            f"UPDATE {schema}.usage_event SET {ROLE_SNAPSHOT_COLUMN} = :names "
            f"WHERE project_id = :project_id AND user_id = :user_id "
            f"AND {ROLE_SNAPSHOT_COLUMN} IS NULL"
        ).bindparams(bindparam("names", type_=ARRAY(Text)))
        #
        stamped_users = 0
        failed_projects = []
        unresolved_users = 0
        #
        for project_id, user_ids in users_by_project.items():
            try:
                names_by_user = an.fetch_project_role_name_map(project_id)
            except Exception:  # pylint: disable=W0703
                log.exception("usage: role lookup failed for project %s, skipped", project_id)
                failed_projects.append(project_id)
                continue
            #
            params = [
                {"names": names_by_user[user_id], "project_id": project_id, "user_id": user_id}
                for user_id in user_ids if names_by_user.get(user_id)
            ]
            unresolved_users += len(user_ids) - len(params)
            #
            if not params:
                continue
            #
            with db.engine.connect() as connection:
                connection.execute(update, params)
                connection.commit()
            #
            stamped_users += len(params)
        #
        log.info(
            "usage: role_snapshot backfill stamped %s user(s) across %s project(s); "
            "%s user(s) with no current role left NULL; failed projects: %s",
            stamped_users, len(users_by_project), unresolved_users, failed_projects,
        )
        #
        return {
            "stamped_users": stamped_users,
            "projects": len(users_by_project),
            "unresolved_users": unresolved_users,
            "failed_projects": failed_projects,
        }

    @web.method()
    def usage_ensure_skill_index(self):
        """Built per partition CONCURRENTLY: a plain CREATE INDEX on the parent blocks inserts."""
        schema = c.POSTGRES_SCHEMA
        built = []
        #
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(parent_index_statement(schema)))
            partitions = [row[0] for row in connection.execute(_PARTITIONS_SQL, {"schema": schema})]
            #
            for partition in partitions:
                if connection.execute(_ATTACHED_SQL, {
                        "schema": schema, "index": SKILL_INDEX_NAME, "partition": partition,
                }).first():
                    continue
                #
                index = partition_index_name(partition)
                if connection.execute(_INVALID_SQL, {"schema": schema, "index": index}).first():
                    connection.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{index}"))
                #
                connection.execute(text(partition_index_statement(schema, partition)))
                connection.execute(text(attach_statement(schema, partition)))
                built.append(partition)
        #
        log.info("usage: skill index built on %s of %s partition(s)", len(built), len(partitions))
        #
        return {"partitions": len(partitions), "built": built}

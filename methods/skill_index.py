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

""" Partial index for skill usage reads (#6926)

create_all never adds an index to a table that already exists, and a plain CREATE INDEX on
the partitioned parent locks out inserts while every month is scanned. So each partition is
indexed CONCURRENTLY and attached to an invalid parent index created ON ONLY the parent; the
parent turns valid once every partition is attached, and partitions created after that
inherit the index from it.
"""

from sqlalchemy import text

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db, config as c  # pylint: disable=E0401

from ..models.usage_event import SKILL_INDEX_NAME, SKILL_INDEX_PREDICATE

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

# A partition already covered: an index of it attached to the parent skill index, either built
# by an earlier run or cloned by CREATE TABLE ... PARTITION OF once the parent index existed
_ATTACHED_SQL = text(
    "SELECT 1 FROM pg_inherits i "
    "JOIN pg_class parent_index ON parent_index.oid = i.inhparent "
    "JOIN pg_namespace n ON n.oid = parent_index.relnamespace "
    "JOIN pg_index x ON x.indexrelid = i.inhrelid "
    "JOIN pg_class part ON part.oid = x.indrelid "
    "WHERE n.nspname = :schema AND parent_index.relname = :index AND part.relname = :partition"
)

# CREATE INDEX CONCURRENTLY that fails part way leaves an INVALID index behind, which
# IF NOT EXISTS would then skip forever
_INVALID_SQL = text(
    "SELECT 1 FROM pg_class ci JOIN pg_namespace n ON n.oid = ci.relnamespace "
    "JOIN pg_index x ON x.indexrelid = ci.oid "
    "WHERE n.nspname = :schema AND ci.relname = :index AND NOT x.indisvalid"
)


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_ensure_skill_index(self):
        """Build the partial skill index on every partition without blocking inserts.

        Safe to rerun: an attached partition is skipped and an invalid leftover is rebuilt.
        """
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

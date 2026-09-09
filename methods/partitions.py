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

""" usage_event monthly partitions """

from datetime import date

from sqlalchemy import text

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db, config as c  # pylint: disable=E0401


def next_month(year, month):
    """The month after (year, month), rolling the year over at December."""
    return (year + 1, 1) if month == 12 else (year, month + 1)


def partition_name(year, month):
    """Child table name for one month."""
    return f"usage_event_{year:04d}{month:02d}"


def month_bounds(year, month):
    """Half-open [start, end) bounds for a month.

    Inclusive would put the 1st in two partitions — Postgres rejects that, and it double-counts.
    """
    end_year, end_month = next_month(year, month)
    #
    return date(year, month, 1), date(end_year, end_month, 1)


def month_range(from_month, to_month):
    """Every (year, month) from from_month to to_month inclusive; empty when to < from."""
    result = []
    current = tuple(from_month)
    #
    while current <= tuple(to_month):
        result.append(current)
        current = next_month(*current)
    #
    return result


def parent_exists(connection, schema):
    """True when the partitioned parent is present."""
    return connection.execute(
        text(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = 'usage_event' AND c.relkind = 'p'"
        ),
        {"schema": schema},
    ).first() is not None


def partition_statements(schema, from_month, to_month):
    """DDL for each month in the range, idempotent and in chronological order."""
    statements = []
    #
    for year, month in month_range(from_month, to_month):
        start, end = month_bounds(year, month)
        #
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {schema}.{partition_name(year, month)} "
            f"PARTITION OF {schema}.usage_event "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}');"
        )
    #
    return statements


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_ensure_partitions(self, from_month=None, to_month=None):
        """Create the current month's partition plus the configured lookahead.

        No DEFAULT partition: it would silently absorb rows for a month that failed to appear.
        """
        today = date.today()
        #
        if from_month is None:
            from_month = (today.year, today.month)
        #
        if to_month is None:
            ahead = int(self.usage_config().get("partition_ahead_months", 1) or 0)
            to_month = from_month
            #
            for _ in range(max(ahead, 0)):
                to_month = next_month(*to_month)
        #
        statements = partition_statements(c.POSTGRES_SCHEMA, from_month, to_month)
        #
        with db.engine.connect() as connection:
            # Absent when shared metadata is not applied automatically; the operator
            # still has to run the admin create_tables task
            if not parent_exists(connection, c.POSTGRES_SCHEMA):
                log.warning(
                    "usage: %s.usage_event is missing; run the admin create_tables task. "
                    "No partitions created", c.POSTGRES_SCHEMA,
                )
                return 0
            #
            for statement in statements:
                connection.execute(text(statement))
            #
            connection.commit()
        #
        log.info("usage: ensured %s usage_event partition(s)", len(statements))
        #
        return len(statements)

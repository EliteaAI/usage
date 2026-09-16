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

""" usage_event compatibility guard

create_all provisions a new database from the model, but never alters a table that already
exists — so a column added to UsageEvent after a deploy needs applying here. Runs on every
ready(), does nothing once the table matches.
"""

from sqlalchemy import text

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db, config as c  # pylint: disable=E0401

# The stored cost split. Priced at meter time so that editing a model's price changes what the
# next call costs and leaves every call already made alone.
COST_SPLIT_COLUMNS = (
    "input_cost_micro_usd",
    "output_cost_micro_usd",
    "cache_read_cost_micro_usd",
    "cache_creation_cost_micro_usd",
)


def existing_columns(connection, schema, table):
    """Column names present on the table, empty when the table itself is absent."""
    rows = connection.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": schema, "table": table},
    ).scalars().all()
    #
    return set(rows)


def add_column_statement(schema, columns):
    """One ALTER for every missing column: a partitioned parent rewrites its children each time.

    IF NOT EXISTS as well as the caller's check — two pylons can reach this concurrently.
    """
    additions = ", ".join(
        f'ADD COLUMN IF NOT EXISTS "{name}" BIGINT NOT NULL DEFAULT 0' for name in columns
    )
    #
    return f"ALTER TABLE {schema}.usage_event {additions};"


def freeze_cost_split_statement(schema):
    """Give rows that predate the split columns a split, once, and never move it again.

    A row written before the columns existed recorded only its total, so the split has to be
    reconstructed from the catalog — but the total is what was actually charged, so the parts are
    allocated *within* it by their share of the current rates rather than recomputed from those
    rates outright. That keeps the parts summing to the recorded total even for a row priced
    under a rate that has since changed, and once written they stop tracking the catalog like
    every row written from now on. input_cost takes the rounding remainder so the sum is exact.

    Rows with no priced tokens at all keep a zero split, which is what they cost.
    """
    return f"""
        UPDATE {schema}.usage_event AS e
        SET output_cost_micro_usd = w.out_micro,
            cache_read_cost_micro_usd = w.read_micro,
            cache_creation_cost_micro_usd = w.create_micro,
            input_cost_micro_usd = e.cost_micro_usd - w.out_micro - w.read_micro
                                   - w.create_micro
        FROM (
            SELECT u.id,
                   u.ts,
                   ROUND(u.cost_micro_usd * u.w_out / u.w_total)::BIGINT AS out_micro,
                   ROUND(u.cost_micro_usd * u.w_read / u.w_total)::BIGINT AS read_micro,
                   ROUND(u.cost_micro_usd * u.w_create / u.w_total)::BIGINT AS create_micro
            FROM (
                SELECT ev.id,
                       ev.ts,
                       ev.cost_micro_usd,
                       COALESCE(ev.output_tokens, 0)
                           * COALESCE(p.output_cost_per_token, 0) AS w_out,
                       COALESCE(ev.cache_read_tokens, 0)
                           * COALESCE(p.cache_read_input_token_cost,
                                      p.input_cost_per_token, 0) AS w_read,
                       COALESCE(ev.cache_creation_tokens, 0)
                           * COALESCE(p.cache_creation_input_token_cost,
                                      p.input_cost_per_token, 0) AS w_create,
                       COALESCE(ev.billable_input_tokens, 0)
                           * COALESCE(p.input_cost_per_token, 0)
                       + COALESCE(ev.output_tokens, 0)
                           * COALESCE(p.output_cost_per_token, 0)
                       + COALESCE(ev.cache_read_tokens, 0)
                           * COALESCE(p.cache_read_input_token_cost,
                                      p.input_cost_per_token, 0)
                       + COALESCE(ev.cache_creation_tokens, 0)
                           * COALESCE(p.cache_creation_input_token_cost,
                                      p.input_cost_per_token, 0) AS w_total
                FROM {schema}.usage_event AS ev
                LEFT JOIN {schema}.model_prices AS p ON p.model_name = ev.model_name
                WHERE ev.cost_micro_usd > 0
                  AND ev.input_cost_micro_usd = 0
                  AND ev.output_cost_micro_usd = 0
                  AND ev.cache_read_cost_micro_usd = 0
                  AND ev.cache_creation_cost_micro_usd = 0
            ) AS u
            WHERE u.w_total > 0
        ) AS w
        WHERE e.id = w.id AND e.ts = w.ts;
    """


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_ensure_event_columns(self):
        """Apply any UsageEvent column the live table is missing; returns how many were added."""
        schema = c.POSTGRES_SCHEMA
        #
        with db.engine.connect() as connection:
            present = existing_columns(connection, schema, "usage_event")
            #
            if not present:
                # shared provisions the table; usage_ensure_partitions already warns when it
                # has not happened yet, so this stays quiet
                return 0
            #
            missing = [name for name in COST_SPLIT_COLUMNS if name not in present]
            #
            if not missing:
                return 0
            #
            connection.execute(text(add_column_statement(schema, missing)))
            connection.commit()
        #
        log.info("usage: added %s usage_event column(s): %s", len(missing), ", ".join(missing))
        #
        self.usage_freeze_cost_split()
        #
        return len(missing)

    @web.method()
    def usage_freeze_cost_split(self):
        """Backfill the split for rows that predate it; a no-op on every later call.

        Separate from the column guard so an operator can re-run it after the costs catalog is
        first populated — a row whose model had no price at all when the columns landed is left
        for that next run rather than frozen at zero.
        """
        schema = c.POSTGRES_SCHEMA
        #
        with db.engine.connect() as connection:
            if not existing_columns(connection, schema, "model_prices"):
                log.warning(
                    "usage: %s.model_prices is absent; the cost split of rows written before "
                    "it was stored stays zero", schema,
                )
                return 0
            #
            result = connection.execute(text(freeze_cost_split_statement(schema)))
            connection.commit()
        #
        frozen = result.rowcount or 0
        #
        if frozen:
            log.info("usage: froze the cost split of %s pre-existing usage_event row(s)", frozen)
        #
        return frozen

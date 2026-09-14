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

""" Counter reconcile — usage_counter against the usage_event facts it summarises """

import datetime

from sqlalchemy import func, select

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db  # pylint: disable=E0401

from ._counters import ALL_MODELS_SENTINEL, PERIOD_MONTH, PROJECT_USER_SENTINEL, period_start
from .drainer import counter_upsert
from ..models.usage_counter import UsageCounter
from ..models.usage_event import UsageEvent

MEASURES = ("input_tokens", "output_tokens", "cost_micro_usd", "call_count")


def _accumulate(totals, key, measured):
    bucket = totals.setdefault(key, {measure: 0 for measure in MEASURES})
    #
    for measure in MEASURES:
        bucket[measure] += measured[measure]

def _repair_row(row):
    """The delta that closes the gap; the upsert accumulates, so it is a difference."""
    return {
        "project_id": row["project_id"],
        "user_id": row["user_id"],
        "period_kind": PERIOD_MONTH,
        "period_start": row["period_start"],
        "model_name": ALL_MODELS_SENTINEL,
        **{
            measure: row["expected"][measure] - row["actual"][measure]
            for measure in MEASURES
        },
    }



def period_bounds(period):
    """[start, next_start) of a 'YYYYMM' period, as UTC datetimes."""
    year, month = int(str(period)[:4]), int(str(period)[4:6])
    start = datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc)
    #
    if month == 12:
        return start, datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    #
    return start, datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc)


def current_period():
    """ Helper """
    return f"{datetime.datetime.now(datetime.timezone.utc):%Y%m}"


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_reconcile_counters(self, period=None, apply=False):  # pylint: disable=W0622
        """Drift between the counters and the facts. Reports only unless apply is set."""
        period = str(period or current_period())
        start, end = period_bounds(period)
        #
        try:
            with db.engine.connect() as connection:
                drift = self.usage_counter_drift(connection, start, end)
                #
                if apply and drift:
                    for row in drift:
                        connection.execute(counter_upsert(_repair_row(row)))
                    #
                    connection.commit()
        except:  # pylint: disable=W0702
            log.exception("usage: reconcile failed for period %s", period)
            return {"period": period, "applied": False, "drift": None}
        #
        if drift:
            log.warning("usage: %s counter row(s) drift in period %s", len(drift), period)
        #
        return {"period": period, "applied": bool(apply), "drift": drift}

    @web.method()
    def usage_counter_drift(self, connection, start, end):
        """Rows where the counters and the facts disagree, facts taken as the truth."""
        facts = self.usage_fact_totals(connection, start, end)
        counters = self.usage_counter_totals(connection, start.date())
        #
        drift = []
        #
        for key, fact in facts.items():
            counter = counters.get(key) or {measure: 0 for measure in MEASURES}
            #
            if any(fact[measure] != counter[measure] for measure in MEASURES):
                drift.append({
                    "project_id": key[0], "user_id": key[1], "period_start": start.date(),
                    "expected": fact, "actual": counter,
                })
        #
        return drift

    @web.method()
    def usage_fact_totals(self, connection, start, end):
        """Per-project and per-member sums straight off usage_event."""
        statement = select(
            UsageEvent.project_id, UsageEvent.user_id,
            func.sum(UsageEvent.input_tokens), func.sum(UsageEvent.output_tokens),
            func.sum(UsageEvent.cost_micro_usd), func.count(),
        ).where(UsageEvent.ts >= start, UsageEvent.ts < end).group_by(
            UsageEvent.project_id, UsageEvent.user_id,
        )
        #
        totals = {}
        #
        for project_id, user_id, inputs, outputs, cost, calls in connection.execute(statement):
            measured = {
                "input_tokens": int(inputs or 0), "output_tokens": int(outputs or 0),
                "cost_micro_usd": int(cost or 0), "call_count": int(calls or 0),
            }
            #
            _accumulate(totals, (project_id, PROJECT_USER_SENTINEL), measured)
            #
            if user_id:
                _accumulate(totals, (project_id, user_id), measured)
        #
        return totals

    @web.method()
    def usage_counter_totals(self, connection, period_day):
        """The aggregate counter rows for the period, keyed the same way as the fact totals."""
        statement = select(
            UsageCounter.project_id, UsageCounter.user_id,
            UsageCounter.input_tokens, UsageCounter.output_tokens,
            UsageCounter.cost_micro_usd, UsageCounter.call_count,
        ).where(
            UsageCounter.period_kind == PERIOD_MONTH,
            UsageCounter.period_start == period_day,
            UsageCounter.model_name == ALL_MODELS_SENTINEL,
        )
        #
        return {
            (project_id, user_id): {
                "input_tokens": int(inputs or 0), "output_tokens": int(outputs or 0),
                "cost_micro_usd": int(cost or 0), "call_count": int(calls or 0),
            }
            for project_id, user_id, inputs, outputs, cost, calls
            in connection.execute(statement)
        }

    @web.method()
    def usage_reconcile_period_start(self, period):
        """ Method """
        return period_start(period_bounds(period)[0], PERIOD_MONTH)

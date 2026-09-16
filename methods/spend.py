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

""" Spend aggregations — the SQL behind rpc/facade.py

Scalar and map spend come off usage_counter (full-PK equality lookups); per-model, per-day and
cache-token detail comes off usage_event, which is the only place that grain exists.
"""

from sqlalchemy import func, select

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db  # pylint: disable=E0401

from ._analytics import total_tokens_expr
from ._counters import (
    ALL_MODELS_SENTINEL, EVENT_TYPE_LLM, PERIOD_MONTH, PROJECT_USER_SENTINEL, period_start,
)
from .reconcile import current_period, period_bounds
from ..models.usage_counter import UsageCounter
from ..models.usage_event import UsageEvent
from ..rpc.facade import empty_spend, empty_usage_detail

# Defensive: callers page far below this, but an IN () list must not grow unbounded
ID_CHUNK = 1000

MICRO = 1_000_000


def _resolve(period):
    """A period resolved once per entrypoint, so a month rollover mid-call cannot straddle."""
    period = str(period or current_period())
    start, end = period_bounds(period)
    #
    return period, start, end, period_start(start, PERIOD_MONTH)


def _dollars(micro):
    """Integer micro-dollars to float, divided once on the final sum."""
    return float(int(micro or 0)) / MICRO


def _chunks(ids):
    values = [int(value) for value in ids or []]
    #
    return [values[at:at + ID_CHUNK] for at in range(0, len(values), ID_CHUNK)]


def _counter_measures(period_day, *where):
    """Summed counter measures for whatever slice the caller pins down."""
    return select(
        func.sum(UsageCounter.input_tokens), func.sum(UsageCounter.output_tokens),
        func.sum(UsageCounter.cost_micro_usd), func.sum(UsageCounter.call_count),
    ).where(
        UsageCounter.period_kind == PERIOD_MONTH,
        UsageCounter.period_start == period_day,
        UsageCounter.model_name == ALL_MODELS_SENTINEL,
        *where,
    )


def _spend_shape(tag, period, row):
    """The legacy spend shape, filled from a counter row.

    total_tokens is input+output because the counter carries no cache buckets. Budget callers
    read `spend`; the Usage page's token figures come off usage_event, where the definition
    matches Analytics.
    """
    inputs, outputs, cost, _calls = row
    #
    return {
        "tag": tag,
        "period": period,
        "spend": _dollars(cost),
        "prompt_tokens": int(inputs or 0),
        "completion_tokens": int(outputs or 0),
        "total_tokens": int(inputs or 0) + int(outputs or 0),
        "available": True,
    }


def _event_where(project_id, start, end, user_id=None):
    clauses = [
        UsageEvent.project_id == int(project_id),
        UsageEvent.ts >= start, UsageEvent.ts < end,
        UsageEvent.event_type == EVENT_TYPE_LLM,
    ]
    #
    if user_id is not None:
        clauses.append(UsageEvent.user_id == int(user_id))
    #
    return clauses


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_read_project_spend(self, project_id, period=None, **_kwargs):
        """Month spend for a project, off the project-aggregate counter row."""
        period, _start, _end, period_day = _resolve(period)
        tag = f"project-{project_id}-{period}"
        #
        try:
            with db.engine.connect() as connection:
                row = connection.execute(_counter_measures(
                    period_day,
                    UsageCounter.project_id == int(project_id),
                    UsageCounter.user_id == PROJECT_USER_SENTINEL,
                )).first()
        except:  # pylint: disable=W0702
            log.exception("usage: project spend read failed for project %s", project_id)
            return empty_spend(tag)
        #
        return _spend_shape(tag, period, row or (0, 0, 0, 0))

    @web.method()
    def usage_read_user_spend(self, project_id, user_id, period=None, **_kwargs):
        """Month spend for one member of a project."""
        period, _start, _end, period_day = _resolve(period)
        tag = f"user-{user_id}-{period}"
        #
        try:
            with db.engine.connect() as connection:
                row = connection.execute(_counter_measures(
                    period_day,
                    UsageCounter.project_id == int(project_id),
                    UsageCounter.user_id == int(user_id),
                )).first()
        except:  # pylint: disable=W0702
            log.exception(
                "usage: member spend read failed for project %s user %s", project_id, user_id,
            )
            return empty_spend(tag)
        #
        return _spend_shape(tag, period, row or (0, 0, 0, 0))

    @web.method()
    def usage_read_projects_spend(self, project_ids, period=None, **_kwargs):
        """Month spend per project id. Every requested id is present, with 0.0 when unmetered.

        A map has no `available` flag, so a failed read answers all-zero, never a partial map.
        """
        _period, _start, _end, period_day = _resolve(period)
        spend = {}
        #
        try:
            chunks = _chunks(project_ids)
            spend = {value: 0.0 for chunk in chunks for value in chunk}
            #
            with db.engine.connect() as connection:
                for chunk in chunks:
                    statement = select(
                        UsageCounter.project_id, func.sum(UsageCounter.cost_micro_usd),
                    ).where(
                        UsageCounter.period_kind == PERIOD_MONTH,
                        UsageCounter.period_start == period_day,
                        UsageCounter.model_name == ALL_MODELS_SENTINEL,
                        UsageCounter.user_id == PROJECT_USER_SENTINEL,
                        UsageCounter.project_id.in_(chunk),
                    ).group_by(UsageCounter.project_id)
                    #
                    for project_id, cost in connection.execute(statement):
                        spend[int(project_id)] = _dollars(cost)
        except:  # pylint: disable=W0702
            log.exception("usage: batched project spend read failed")
            # All-zero rather than the partial map: a caller cannot tell a real 0.0 from a
            # dropped chunk, so a half-read must not look like a complete answer
            return dict.fromkeys(spend, 0.0)
        #
        return spend

    @web.method()
    def usage_read_users_spend(self, project_id, user_ids, period=None, **_kwargs):
        """Month spend per member of one project, keyed by user id.

        A failed read answers all-zero for every requested id, never a partial map.
        """
        _period, _start, _end, period_day = _resolve(period)
        spend = {}
        #
        try:
            chunks = _chunks(user_ids)
            spend = {value: 0.0 for chunk in chunks for value in chunk}
            #
            with db.engine.connect() as connection:
                for chunk in chunks:
                    statement = select(
                        UsageCounter.user_id, func.sum(UsageCounter.cost_micro_usd),
                    ).where(
                        UsageCounter.period_kind == PERIOD_MONTH,
                        UsageCounter.period_start == period_day,
                        UsageCounter.model_name == ALL_MODELS_SENTINEL,
                        UsageCounter.project_id == int(project_id),
                        UsageCounter.user_id.in_(chunk),
                    ).group_by(UsageCounter.user_id)
                    #
                    for user_id, cost in connection.execute(statement):
                        spend[int(user_id)] = _dollars(cost)
        except:  # pylint: disable=W0702
            log.exception("usage: batched member spend read failed for project %s", project_id)
            return dict.fromkeys(spend, 0.0)
        #
        return spend

    @web.method()
    def usage_read_member_spend_listing(self, project_id, period=None, **_kwargs):
        """Members with recorded spend plus the project total. None means unreachable."""
        _period, _start, _end, period_day = _resolve(period)
        #
        statement = select(
            UsageCounter.user_id, UsageCounter.cost_micro_usd, UsageCounter.call_count,
        ).where(
            UsageCounter.period_kind == PERIOD_MONTH,
            UsageCounter.period_start == period_day,
            UsageCounter.model_name == ALL_MODELS_SENTINEL,
            UsageCounter.project_id == int(project_id),
        )
        #
        try:
            with db.engine.connect() as connection:
                rows = list(connection.execute(statement))
        except:  # pylint: disable=W0702
            log.exception("usage: member spend listing failed for project %s", project_id)
            return None
        #
        members = {}
        project_spend = 0.0
        #
        for user_id, cost, calls in rows:
            if int(user_id) == PROJECT_USER_SENTINEL:
                project_spend = _dollars(cost)
                continue
            #
            members[int(user_id)] = {"spend": _dollars(cost), "requests": int(calls or 0)}
        #
        return {"project": {"spend": project_spend}, "members": members}

    @web.method()
    def usage_read_project_usage_detail(self, project_id, period=None, **_kwargs):
        """Per-model and per-day month usage for a project."""
        period, start, end, _day = _resolve(period)
        #
        return self.usage_event_detail(
            f"project-{project_id}-{period}", period, _event_where(project_id, start, end),
        )

    @web.method()
    def usage_read_user_usage_detail(self, project_id, user_id, period=None, **_kwargs):
        """Per-model and per-day month usage for one member of a project."""
        period, start, end, _day = _resolve(period)
        #
        return self.usage_event_detail(
            f"user-{user_id}-{period}", period,
            _event_where(project_id, start, end, user_id=user_id),
        )

    @web.method()
    def usage_event_detail(self, tag, period, where):
        """Totals, per-model and per-day rows off usage_event for one slice.

        The per-day date_trunc grouping is not index-backed on purpose — no functional index
        is added for it; the row filter is what the index serves.
        """
        # total_tokens_expr, not input+output: the Usage page and Analytics have to report the
        # same number for the same rows, so the definition lives in one place
        totals = select(
            func.sum(UsageEvent.input_tokens), func.sum(UsageEvent.output_tokens),
            func.sum(UsageEvent.cache_read_tokens), func.sum(UsageEvent.cache_creation_tokens),
            func.sum(UsageEvent.cost_micro_usd), func.count(),
            func.sum(total_tokens_expr()),
        ).where(*where)
        #
        # The table ranks rows and draws share bars in payload order, so the sort is the server's
        by_model = select(
            UsageEvent.model_name,
            func.sum(UsageEvent.cost_micro_usd),
            func.sum(total_tokens_expr()),
            func.count(),
        ).where(*where).group_by(UsageEvent.model_name).order_by(
            func.sum(UsageEvent.cost_micro_usd).desc(), UsageEvent.model_name,
        )
        #
        day = func.date_trunc("day", UsageEvent.ts)
        # tokens and calls too: the chart's own "has data" check reads api_requests
        by_day = select(
            day, func.sum(UsageEvent.cost_micro_usd),
            func.sum(total_tokens_expr()),
            func.count(),
        ).where(*where).group_by(day).order_by(day)
        #
        try:
            with db.engine.connect() as connection:
                inputs, outputs, cache_read, cache_creation, cost, calls, tokens = \
                    connection.execute(totals).first() or (0, 0, 0, 0, 0, 0, 0)
                models = [
                    {
                        "model": model_name or "",
                        "spend": _dollars(model_cost),
                        "total_tokens": int(tokens or 0),
                        "api_requests": int(model_calls or 0),
                    }
                    for model_name, model_cost, tokens, model_calls
                    in connection.execute(by_model)
                ]
                daily = [
                    {
                        "date": moment.date().isoformat(),
                        "spend": _dollars(day_cost),
                        "total_tokens": int(day_tokens or 0),
                        "api_requests": int(day_calls or 0),
                    }
                    for moment, day_cost, day_tokens, day_calls
                    in connection.execute(by_day)
                ]
        except:  # pylint: disable=W0702
            log.exception("usage: usage detail read failed for %s", tag)
            return empty_usage_detail(tag)
        #
        return {
            "tag": tag,
            "period": period,
            "models": models,
            "daily": daily,
            "spend": _dollars(cost),
            "total_tokens": int(tokens or 0),
            "input_tokens": int(inputs or 0),
            "output_tokens": int(outputs or 0),
            "cache_read_tokens": int(cache_read or 0),
            "cache_creation_tokens": int(cache_creation or 0),
            "api_requests": int(calls or 0),
            "available": True,
        }

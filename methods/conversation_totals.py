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

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db  # pylint: disable=E0401

from ._analytics import METERED_EVENT_TYPES, total_tokens_expr
from ._counters import NANO
from .mode import MODE_OFF
from ..models.usage_event import UsageEvent

ID_CHUNK = 1000


def _chunks(ids):
    values = [str(value) for value in ids or []]
    return [values[at:at + ID_CHUNK] for at in range(0, len(values), ID_CHUNK)]


def root_entity_conditions(root_entity_type, root_entity_id, root_entity_project_id):
    conditions = []
    if root_entity_type:
        conditions.append(UsageEvent.root_entity_type == root_entity_type)
    if root_entity_id is not None:
        conditions.append(UsageEvent.root_entity_id == int(root_entity_id))
    if root_entity_project_id is not None:
        conditions.append(
            func.coalesce(UsageEvent.root_entity_project_id, UsageEvent.project_id)
            == int(root_entity_project_id)
        )
    return conditions


def conversation_totals_statement(project_id, conversation_ids, entity_conditions):
    return select(
        UsageEvent.conversation_id,
        func.sum(total_tokens_expr()),
        func.sum(UsageEvent.cost_nano_usd),
        func.array_remove(func.array_agg(func.distinct(UsageEvent.model_name)), None),
        func.bool_or(UsageEvent.is_error),
        func.array_agg(aggregate_order_by(UsageEvent.run_id, UsageEvent.ts.desc()))[1],
    ).where(
        UsageEvent.project_id == int(project_id),
        UsageEvent.conversation_id.in_(conversation_ids),
        UsageEvent.event_type.in_(METERED_EVENT_TYPES),
        *entity_conditions,
    ).group_by(UsageEvent.conversation_id)


def conversation_totals_row(row):
    _conversation_id, tokens, cost, models, has_error, last_run_id = row
    return {
        "tokens": int(tokens or 0),
        "cost": int(cost or 0) / NANO,
        "models": sorted(models or []),
        "has_error": bool(has_error),
        "last_run_id": last_run_id,
    }


def root_entity_models_statement(project_id, conversation_ids, entity_conditions):
    return select(func.distinct(UsageEvent.model_name)).where(
        UsageEvent.project_id == int(project_id),
        UsageEvent.conversation_id.in_(conversation_ids),
        UsageEvent.event_type.in_(METERED_EVENT_TYPES),
        UsageEvent.model_name.isnot(None),
        *entity_conditions,
    )


def root_entity_conversations_statement(project_id, model_name, entity_conditions):
    return select(func.distinct(UsageEvent.conversation_id)).where(
        UsageEvent.project_id == int(project_id),
        UsageEvent.event_type.in_(METERED_EVENT_TYPES),
        UsageEvent.model_name == model_name,
        UsageEvent.conversation_id.isnot(None),
        *entity_conditions,
    )


class Method:  # pylint: disable=E1101,R0903,W0201

    @web.method()
    def usage_read_conversation_totals(  # pylint: disable=R0913
            self, project_id, conversation_ids, root_entity_type=None, root_entity_id=None,
            root_entity_project_id=None, **_kwargs,
    ):
        if self.usage_get_mode() == MODE_OFF:
            return None
        entity_conditions = root_entity_conditions(
            root_entity_type, root_entity_id, root_entity_project_id,
        )
        totals = {}
        try:
            with db.engine.connect() as connection:
                for chunk in _chunks(conversation_ids):
                    statement = conversation_totals_statement(project_id, chunk, entity_conditions)
                    for row in connection.execute(statement):
                        totals[row[0]] = conversation_totals_row(row)
        except:  # pylint: disable=W0702
            log.exception("usage: conversation totals read failed for project %s", project_id)
            return None
        return totals

    @web.method()
    def usage_read_root_entity_models(  # pylint: disable=R0913
            self, project_id, conversation_ids, root_entity_type, root_entity_id,
            root_entity_project_id=None, **_kwargs,
    ):
        if self.usage_get_mode() == MODE_OFF:
            return None
        entity_conditions = root_entity_conditions(
            root_entity_type, root_entity_id, root_entity_project_id,
        )
        models = set()
        try:
            with db.engine.connect() as connection:
                for chunk in _chunks(conversation_ids):
                    statement = root_entity_models_statement(project_id, chunk, entity_conditions)
                    models.update(model_name for (model_name,) in connection.execute(statement))
        except:  # pylint: disable=W0702
            log.exception("usage: entity models read failed for project %s", project_id)
            return None
        return sorted(models)

    @web.method()
    def usage_read_root_entity_conversations(  # pylint: disable=R0913
            self, project_id, model_name, root_entity_type, root_entity_id,
            root_entity_project_id=None, **_kwargs,
    ):
        if self.usage_get_mode() == MODE_OFF:
            return None
        entity_conditions = root_entity_conditions(
            root_entity_type, root_entity_id, root_entity_project_id,
        )
        try:
            with db.engine.connect() as connection:
                statement = root_entity_conversations_statement(project_id, model_name, entity_conditions)
                return [conversation_id for (conversation_id,) in connection.execute(statement)]
        except:  # pylint: disable=W0702
            log.exception("usage: entity conversations read failed for project %s", project_id)
            return None

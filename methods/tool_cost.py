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
""" tool_cost rows: the ledger side of the Tool Spend framework (check/record live in elitea_core) """

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from ._counters import EVENT_TYPE_TOOL_COST, NANO
from .mode import MODE_OFF

# Counter meta keys that may land on the row; nothing else (no queries, no URLs)
TOOL_COST_META_KEYS = (
    "feature", "backend", "op", "units", "unit", "credits_source",
    "results_count", "urls_count", "failed_urls", "depth", "period_kind",
)
REQUIRED_KEYS = ("ts", "idempotency_key", "project_id", "feature", "backend", "op")


def tool_cost_row(record, attribution):
    """usage_event payload for one recorded tool spend; ts and idempotency_key are kept as given."""
    meta = {key: record[key] for key in TOOL_COST_META_KEYS if record.get(key) is not None}
    meta.update({
        key: value for key, value in (record.get("meta") or {}).items()
        if key in TOOL_COST_META_KEYS and key not in meta
    })
    cost = record.get("cost_usd")
    #
    row = {
        **(attribution or {}),
        "ts": record["ts"],
        "idempotency_key": record["idempotency_key"],
        "project_id": int(record["project_id"]),
        "user_id": record.get("user_id"),
        "user_email": record.get("user_email"),
        "run_id": record.get("run_id"),
        "event_type": EVENT_TYPE_TOOL_COST,
        "tool_name": record.get("tool_name"),
        "model_name": f"{record['backend']}/{record['op']}",
        "cost_nano_usd": int(round(cost * NANO)) if cost is not None else 0,
        "cost_source": record.get("cost_source") if cost is not None else "unpriced",
        "duration_ms": record.get("duration_ms"),
        "is_error": not record.get("ok", True),
        "meta": meta,
    }
    if record.get("budget_exempt"):
        row["budget_exempt"] = True
    return row


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_record_tool_cost(self, record, attribution=None):
        """Queue one tool_cost row; a replayed permit is dropped by (idempotency_key, ts)."""
        if self.usage_get_mode() == MODE_OFF:
            return {"recorded": False, "duplicate": False}
        #
        missing = [key for key in REQUIRED_KEYS if record.get(key) in (None, "")]
        if missing:
            raise ValueError(f"tool_cost record is missing {missing}")
        #
        from .. import hooks  # pylint: disable=C0415
        try:
            verified = hooks._attribution(attribution, None, record["project_id"])  # pylint: disable=W0212
        except:  # pylint: disable=W0702
            log.warning("usage: dropping unverifiable attribution on a tool_cost row")
            verified = {}
        #
        row = tool_cost_row(record, verified)
        if self.usage_enqueue_event(row):
            return {"recorded": True, "duplicate": False}
        landed = self.usage_write_event(row)
        return {"recorded": True, "duplicate": not landed}

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

import math

from ._counters import EVENT_TYPE_TOOL_COST, NANO
from .mode import MODE_OFF

REQUIRED_KEYS = ("ts", "idempotency_key", "project_id", "feature", "backend", "op")
# Storage bounds of the target columns (bigint nano-USD, int4 ms/counts), not tuning knobs
BIGINT_MAX = 2**63 - 1
INT32_MAX = 2**31 - 1
MAX_NAME = 64
# Typed meta: key -> validator. Anything else (queries, URLs, nested objects) never lands.
META_FIELDS = {
    "credits_source": lambda v: v in ("reported", "estimated"),
    "depth": lambda v: v in ("basic", "advanced"),
    "results_count": lambda v: _count(v),
    "urls_count": lambda v: _count(v),
    "failed_urls": lambda v: _count(v),
}


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= INT32_MAX


def _money(value, name):
    """Finite and >= 0: a ledger row can only ever add spend."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"tool_cost {name} must be a finite number >= 0")
    return float(value)


def _name(value, name):
    if not isinstance(value, str) or not 0 < len(value) <= MAX_NAME:
        raise ValueError(f"tool_cost {name} must be a short string")
    return value


def tool_cost_row(record, attribution):
    """usage_event payload for one recorded tool spend; ts and idempotency_key are kept as given."""
    feature, backend, op = (_name(record[key], key) for key in ("feature", "backend", "op"))
    units = _money(record.get("units") or 0, "units")
    cost = record.get("cost_usd")
    cost_nano = 0 if cost is None else int(round(_money(cost, "cost_usd") * NANO))
    if cost_nano > BIGINT_MAX:
        raise ValueError("tool_cost cost_usd is out of range")
    duration = record.get("duration_ms")
    if duration is not None and not _count(duration):
        raise ValueError("tool_cost duration_ms must be an int >= 0")
    meta = {"feature": feature, "backend": backend, "op": op, "units": units, "unit": _name(record.get("unit") or "credit", "unit")}
    meta.update({
        key: value for key, value in (record.get("meta") or {}).items()
        if key in META_FIELDS and META_FIELDS[key](value)
    })
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
        "tool_name": (record.get("tool_name") or None) and str(record["tool_name"])[:256],
        "model_name": f"{backend}/{op}",
        "cost_nano_usd": cost_nano,
        "cost_source": record.get("cost_source") if cost is not None else "unpriced",
        "duration_ms": duration,
        "is_error": record.get("ok", True) is not True,
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

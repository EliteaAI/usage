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

""" usage_counter key helpers (underscored: pure helpers, not a pylon Method module) """

from datetime import date, datetime, timezone

PERIOD_MONTH = "month"

# user_id 0 is the project aggregate; model_name '' is all models. Reserved, never a real row.
PROJECT_USER_SENTINEL = 0
ALL_MODELS_SENTINEL = ""

# The one event type that feeds usage_counter, so the page total, the gate total and the drift
# report can never disagree. Spelled once here rather than per module.
EVENT_TYPE_LLM = "llm"

# Stored money unit: nano-USD per dollar. Micro rounded sub-$0.0000005 embedding calls to 0.
NANO = 1_000_000_000


def period_start(moment, period_kind=PERIOD_MONTH):
    """First day of the period, as a date — the PK column is DATE, a datetime never matches.

    Naive input is read as UTC; the ledger's month boundary is UTC everywhere.
    """
    if period_kind != PERIOD_MONTH:
        raise ValueError(f"Unsupported period kind: {period_kind!r}")
    #
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        #
        moment = moment.astimezone(timezone.utc).date()
    #
    if not isinstance(moment, date):
        raise TypeError(f"Expected date or datetime, got {type(moment).__name__}")
    #
    return moment.replace(day=1)


def project_key(project_id, moment, model_name=ALL_MODELS_SENTINEL, period_kind=PERIOD_MONTH):
    """Counter key for a project total."""
    return {
        "project_id": int(project_id),
        "user_id": PROJECT_USER_SENTINEL,
        "period_kind": period_kind,
        "period_start": period_start(moment, period_kind),
        "model_name": model_name or ALL_MODELS_SENTINEL,
    }


def member_key(
        project_id, user_id, moment,
        model_name=ALL_MODELS_SENTINEL, period_kind=PERIOD_MONTH,
):
    """Counter key for one member's slice of a project."""
    if int(user_id) == PROJECT_USER_SENTINEL:
        raise ValueError("user_id 0 is reserved for the project aggregate")
    #
    return {
        "project_id": int(project_id),
        "user_id": int(user_id),
        "period_kind": period_kind,
        "period_start": period_start(moment, period_kind),
        "model_name": model_name or ALL_MODELS_SENTINEL,
    }

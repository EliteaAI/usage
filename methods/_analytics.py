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

""" Analytics helpers over usage_event (underscored: pure helpers, not a pylon Method module)

usage_event lives in the shared schema, so every reader filters on the project_id column and
there is no per-project schema to switch into. The project_id on a row is the project that
actually ran the call, which is what stops a forked agent's usage from leaking back into the
project it was forked from.

The counting defects #6574 names are fixed once here rather than in each endpoint:
HUMAN_ACTOR drops the synthetic actor, total_tokens_expr counts error rows, and
agent_runs_expr counts runs instead of the calls inside them.
"""

import datetime
import time
import uuid
from typing import NamedTuple, Optional

from sqlalchemy import Date, and_, case, cast, distinct, func, or_, select

from pylon.core.tools import log  # pylint: disable=E0611,E0401

from ._counters import NANO
from ..models.usage_event import UsageEvent

DEFAULT_DATE_RANGE_DAYS = 7
# One row per calendar day, so an unbounded span is unbounded cardinality
MAX_DATE_RANGE_DAYS = 366
RUN_SCOPE_MARGIN = datetime.timedelta(minutes=5)

# A platform-initiated call is recorded under a synthetic actor with no email. It is not a
# project member and must never reach a leaderboard or an adoption denominator.
SYSTEM_USER_ID = 0

EVENT_LLM = "llm"
EVENT_TOOL = "tool"

# What the user launched. entity_* is the node that made one call; root_entity_* is the run it
# belongs to, so a run's agent identity survives on every llm and tool row underneath it.
AGENT_ROOT_TYPES = ("application", "pipeline")

# Marks a judge/eval-batch call (#6677): root_entity_* still names the real application/pipeline
# being evaluated, so this must be excluded from agent-run counts on entity_type, not root type.
ENTITY_TYPE_EVALUATION = "evaluation"

# Only a leaderboard row's own actor matters here — the usage_event project_id already scopes
# the query, so there is no user_email allow-list to maintain beyond the synthetic actor.
SYSTEM_USER_EMAILS = ("system@centry.user",)
SYSTEM_USER_EMAIL_PREFIX = "system_user_"
SYSTEM_USER_EMAIL_SUFFIX = "@centry.user"

# Drops the synthetic actor from anything user-facing
HUMAN_ACTOR = UsageEvent.user_id != SYSTEM_USER_ID

# Scheduled/webhook/index runs still bill their configuring user but are not that user's activity (#6881)
MANUAL_RUN = UsageEvent.trigger_source.is_(None)

# Platform-managed roles that project creation copies into every project's role set (#6794):
# super_admin is seeded into mode="default" and system is the pre-existing per-project role.
# Neither is a role a user picks, so both are hidden from the analytics role filter — mirrors
# admin/constants.py's RESTRICTED_ROLES.
RESTRICTED_ROLES = {"super_admin", "system"}

# Global super-admin verdicts, cached per user_id: {user_id: (monotonic_stamp, is_super_admin)}
SUPER_ADMIN_TTL_SECONDS = 300
SUPER_ADMIN_CACHE_MAX = 2048
_super_admin_cache = {}


def as_utc(value):
    """Aware UTC, or None.

    Two reasons every bound goes through this. A request bound parsed from 'YYYY-MM-DD' is
    naive while the defaults below are aware, and subtracting one from the other raises — so
    supplying only one bound used to 500. And ts is timestamptz, so a naive bound is compared
    in the session timezone rather than UTC, which shifts the window silently.
    """
    if value is None:
        return None
    #
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    #
    return value.astimezone(datetime.timezone.utc)


def parse_date_range(args, run_scope=None):
    """[from, to) bounds from date_from/date_to, defaulted and clamped, always aware UTC.

    A single supplied bound anchors the other rather than widening to everything. A run-scoped
    query with no explicit bounds is not windowed to the last week: the run is the window.
    """
    date_from = args.get("date_from")
    date_to = args.get("date_to")
    #
    try:
        dt_from = as_utc(datetime.datetime.fromisoformat(date_from)) if date_from else None
    except (ValueError, TypeError):
        dt_from = None
    #
    try:
        dt_to = as_utc(datetime.datetime.fromisoformat(date_to)) if date_to else None
    except (ValueError, TypeError):
        dt_to = None
    #
    if run_scope is not None and not dt_from and not dt_to:
        return run_scope.dt_from, run_scope.dt_to
    #
    return clamp_date_range(dt_from, dt_to)


class RunScope(NamedTuple):
    """One run to scope analytics to: an agent/pipeline run, an eval run, or both."""
    # Run History conversation uuid (usage_event.conversation_id), or a raw usage_event.run_id
    run_id: Optional[str] = None
    # p_<pid>.eval_run.id, resolved from the eval run uuid the caller passed
    eval_run_id: Optional[int] = None
    # usage_event.run_id the eval run stamped on its rows; None for runs launched before it was stored
    platform_run_id: Optional[str] = None
    dt_from: Optional[datetime.datetime] = None
    dt_to: Optional[datetime.datetime] = None


def parse_run_scope(project_id, args):
    """RunScope from run_id/eval_run_id, or None when neither is given.

    Raises ValueError on a malformed id and LookupError when the eval run is not in the project.
    """
    raw_run_id = (args.get("run_id") or "").strip()
    raw_eval_run_id = (args.get("eval_run_id") or "").strip()
    #
    if not raw_run_id and not raw_eval_run_id:
        return None
    #
    run_id = None
    if raw_run_id:
        try:
            run_id = str(uuid.UUID(raw_run_id))
        except ValueError as exc:
            raise ValueError("run_id must be a UUID") from exc
    #
    if not raw_eval_run_id:
        return RunScope(run_id=run_id)
    #
    try:
        eval_run_uuid = str(uuid.UUID(raw_eval_run_id))
    except ValueError as exc:
        raise ValueError("eval_run_id must be a UUID") from exc
    #
    from tools import rpc_tools  # pylint: disable=C0415,E0401
    #
    eval_run = rpc_tools.RpcMixin().rpc.timeout(5).elitea_core_eval_run_usage_scope(
        project_id, eval_run_uuid,
    )
    if not eval_run or eval_run.get("id") is None:
        raise LookupError(f"Eval run {eval_run_uuid} not found")
    eval_run_id = int(eval_run["id"])
    #
    dt_from = _parse_utc(eval_run.get("started_at"))
    dt_to = _parse_utc(eval_run.get("finished_at"))
    # Margin for clock skew between the eval worker and the rows it metered
    if dt_from is not None:
        dt_from -= RUN_SCOPE_MARGIN
    if dt_to is not None:
        dt_to += RUN_SCOPE_MARGIN
    #
    platform_run_id = eval_run.get("platform_run_id")
    try:
        platform_run_id = str(uuid.UUID(platform_run_id)) if platform_run_id else None
    except (TypeError, ValueError):
        platform_run_id = None
    #
    return RunScope(
        run_id=run_id,
        eval_run_id=eval_run_id,
        platform_run_id=platform_run_id,
        dt_from=dt_from,
        dt_to=dt_to,
    )


def request_run_scope(project_id, args):
    """(run_scope, None), or (None, (error_body, status)) for a malformed or unknown run."""
    try:
        return parse_run_scope(project_id, args), None
    except ValueError as exc:
        return None, ({"error": str(exc)}, 400)
    except LookupError as exc:
        return None, ({"error": str(exc)}, 404)


# OpenAPI query params every run-scopable analytics endpoint accepts
RUN_SCOPE_PARAMETERS = [
    {
        "name": "run_id",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "format": "uuid"},
        "description": (
            "Scope to one agent/pipeline run: the Run History conversation uuid (a raw usage "
            "run id also matches). Without date_from/date_to the 7-day default window is not applied."
        ),
    },
    {
        "name": "eval_run_id",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "format": "uuid"},
        "description": (
            "Scope to one evaluation run by its uuid. Without date_from/date_to the window is "
            "the eval run's own start/finish."
        ),
    },
]


def _parse_utc(value):
    if not value:
        return None
    #
    try:
        return as_utc(datetime.datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def run_filters(run_scope):
    """Conditions restricting usage_event to the scoped run."""
    if run_scope is None:
        return []
    #
    conditions = []
    #
    if run_scope.run_id:
        # A Run History run is a conversation; usage_event.run_id is minted per message dispatch
        # (utils/run_id.derived_run_id), so it never equals the conversation uuid
        conditions.append(or_(
            UsageEvent.conversation_id == run_scope.run_id,
            UsageEvent.run_id == run_scope.run_id,
        ))
    #
    if run_scope.eval_run_id is not None:
        if run_scope.platform_run_id:
            conditions.append(UsageEvent.run_id == run_scope.platform_run_id)
        else:
            # Runs from before the platform run id was stored: every judge and case call of an
            # eval run is attributed to it as the evaluation entity (#6677)
            conditions.append(UsageEvent.entity_type == ENTITY_TYPE_EVALUATION)
            conditions.append(UsageEvent.entity_id == run_scope.eval_run_id)
    #
    return conditions


def clamp_date_range(dt_from, dt_to):
    """Default and clamp an already-parsed pair, so a caller that got its bounds from
    somewhere other than query args (an RPC) cannot ask for an unbounded span.
    """
    dt_from, dt_to = as_utc(dt_from), as_utc(dt_to)
    #
    if not dt_from and not dt_to:
        dt_to = datetime.datetime.now(datetime.timezone.utc)
        dt_from = dt_to - datetime.timedelta(days=DEFAULT_DATE_RANGE_DAYS)
    elif not dt_from:
        dt_from = dt_to - datetime.timedelta(days=DEFAULT_DATE_RANGE_DAYS)
    elif not dt_to:
        dt_to = datetime.datetime.now(datetime.timezone.utc)
    #
    if (dt_to - dt_from).days > MAX_DATE_RANGE_DAYS:
        dt_from = dt_to - datetime.timedelta(days=MAX_DATE_RANGE_DAYS)
    #
    return dt_from, dt_to


def base_filters(project_id, dt_from, dt_to, human_only=True, run_scope=None):
    """Project and date conditions, leading with (project_id, ts) so the index applies.

    A run-scoped query keeps every row of the run, system actors included, so its totals are
    the run's real totals.
    """
    conditions = [UsageEvent.project_id == project_id]
    #
    if dt_from is not None:
        conditions.append(UsageEvent.ts >= dt_from)
    #
    if dt_to is not None:
        conditions.append(UsageEvent.ts <= dt_to)
    #
    if run_scope is not None:
        conditions.extend(run_filters(run_scope))
    elif human_only:
        conditions.append(HUMAN_ACTOR)
        # HUMAN_ACTOR only drops the user_id=0 sentinel; real system/service accounts have
        # genuine nonzero ids and a recognisable email, so they are dropped here instead.
        conditions.append(or_(
            UsageEvent.user_email.is_(None),
            and_(
                ~UsageEvent.user_email.in_(SYSTEM_USER_EMAILS),
                ~UsageEvent.user_email.like(f"{SYSTEM_USER_EMAIL_PREFIX}%{SYSTEM_USER_EMAIL_SUFFIX}"),
            ),
        ))
    #
    return conditions


def total_tokens_expr():
    """Every token bucket, counted once.

    billable_input_tokens rather than input_tokens: under the inclusive cache convention
    cache_read_tokens is already part of input_tokens, and billable_* is the column with that
    convention normalised away, so adding the cache buckets here cannot double count.
    reasoning_tokens is left out for the same reason — every dialect reports it inside
    output_tokens.

    Error rows are counted: a provider that reported tokens alongside a 4xx has charged for
    them, and hiding that made failed traffic free on the dashboard.
    """
    return (
        func.coalesce(UsageEvent.billable_input_tokens, 0)
        + func.coalesce(UsageEvent.output_tokens, 0)
        + func.coalesce(UsageEvent.cache_read_tokens, 0)
        + func.coalesce(UsageEvent.cache_creation_tokens, 0)
    )


def count_where(condition):
    """Rows matching condition, as a sum over the same single scan."""
    return func.sum(case((condition, 1), else_=0))


def llm_calls_expr():
    """ Helper """
    return count_where(UsageEvent.event_type == EVENT_LLM)


def tool_runs_expr():
    """ Helper """
    return count_where(UsageEvent.event_type == EVENT_TOOL)


def agent_runs_expr(run_scope=None):
    """Distinct runs, not the calls inside them.

    One agent run makes many llm and tool calls; summing rows reported each of them as a
    separate run and inflated the figure by whatever the agent's fan-out happened to be.
    """
    return func.count(distinct(case(
        (is_agent_row(run_scope), UsageEvent.run_id),
        else_=None,
    )))


def agent_error_runs_expr(run_scope=None):
    """Runs that contained at least one failed call.

    Pairs with agent_runs_expr: counting failed calls against a run count would let an
    error_rate exceed 100% whenever one run failed repeatedly.
    """
    return func.count(distinct(case(
        (
            and_(is_agent_row(run_scope), UsageEvent.is_error.is_(True)),
            UsageEvent.run_id,
        ),
        else_=None,
    )))


def is_agent_row(run_scope=None):
    """Agent/pipeline runs, excluding evaluation calls against them (#6677).

    Scoped to one eval run, the evaluation calls are the run, so they are kept.
    """
    if run_scope is not None and run_scope.eval_run_id is not None:
        return UsageEvent.root_entity_type.in_(AGENT_ROOT_TYPES)
    #
    return and_(
        UsageEvent.root_entity_type.in_(AGENT_ROOT_TYPES),
        func.coalesce(UsageEvent.entity_type, "") != ENTITY_TYPE_EVALUATION,
    )


def is_evaluation_row():
    """Judge/eval-batch calls (#6677): the spend is_agent_row() leaves out, but the totals keep."""
    return UsageEvent.entity_type == ENTITY_TYPE_EVALUATION


def root_project_expr():
    """Schema the root entity lives in (#6902); a row predating the column is its own project."""
    return func.coalesce(UsageEvent.root_entity_project_id, UsageEvent.project_id)


def _entity_meta_rpc(project_id, **kwargs):
    from tools import rpc_tools  # pylint: disable=C0415,E0401
    #
    try:
        return rpc_tools.RpcMixin().rpc.timeout(5).elitea_core_usage_entity_meta(
            project_id, **kwargs,
        ) or {}
    except:  # pylint: disable=W0702
        log.warning("usage: entity names unavailable for project %s", project_id)
        return {}


def entity_meta(project_id, application_ids=(), version_ids=()):
    """({app_id: meta}, {version_id: meta}) with names and agent/pipeline kind (#6678).

    usage_event carries no root name and root_entity_type is always 'application', so both
    come from elitea_core. A failed lookup degrades to unlabelled rows, not a failed report.
    """
    application_ids = [i for i in application_ids if i is not None]
    version_ids = [i for i in version_ids if i is not None]
    if not application_ids and not version_ids:
        return {}, {}
    #
    response = _entity_meta_rpc(project_id, application_ids=application_ids, version_ids=version_ids)
    #
    return (
        {item["id"]: item for item in response.get("applications") or []},
        {item["id"]: item for item in response.get("versions") or []},
    )


def application_meta(project_id, refs=()):
    """{(root_project_id, app_id): meta} for root_project_expr() groups (#6902).

    An app id is only unique within its own schema, so a public agent is looked up in the
    public project's schema and never matched against this project's same-id application.
    """
    refs = sorted({(p, i) for p, i in refs if p is not None and i is not None})
    if not refs:
        return {}
    #
    response = _entity_meta_rpc(project_id, application_refs=[list(ref) for ref in refs])
    #
    return {(item["project_id"], item["id"]): item for item in response.get("scoped_applications") or []}


def active_users_expr():
    """Distinct human actors."""
    return func.count(distinct(case((and_(HUMAN_ACTOR, MANUAL_RUN), UsageEvent.user_id), else_=None)))


def manual_users_expr():
    """Distinct users of the rows in scope, automated runs aside."""
    return func.count(distinct(case((MANUAL_RUN, UsageEvent.user_id), else_=None)))


def automated_activity(conditions):
    """Runs, calls and cost of automated runs, one entry per trigger_source."""
    rows = fetch_all(select_from(
        [
            UsageEvent.trigger_source,
            func.count(distinct(UsageEvent.run_id)).label("runs"),
            llm_calls_expr().label("llm_calls"),
            tool_runs_expr().label("tool_runs"),
            func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
        ],
        [*conditions, ~MANUAL_RUN],
    ).group_by(UsageEvent.trigger_source).order_by(UsageEvent.trigger_source))
    return [
        {
            "trigger_source": r["trigger_source"],
            "runs": int(r["runs"] or 0),
            "llm_calls": int(r["llm_calls"] or 0),
            "tool_runs": int(r["tool_runs"] or 0),
            "llm_cost": cost_usd(r["cost_nano"] or 0),
        }
        for r in rows
    ]


def day_expr():
    """ Helper """
    return cast(UsageEvent.ts, Date)


def week_expr():
    """Calendar week, Monday-aligned (Postgres date_trunc semantics)."""
    return func.date_trunc("week", UsageEvent.ts)


def month_expr():
    """ Helper """
    return func.date_trunc("month", UsageEvent.ts)


GRANULARITY_DAY = "day"
GRANULARITY_WEEK = "week"
GRANULARITY_MONTH = "month"
# Order matters nowhere here; the dict is only ever read by an already-validated key
_BUCKET_EXPRS = {GRANULARITY_DAY: day_expr, GRANULARITY_WEEK: week_expr, GRANULARITY_MONTH: month_expr}


def parse_granularity(args):
    """day | week | month, defaulted to day. An unrecognised value defaults rather than
    reaching bucket_expr, so a bad query param can never select SQL by string.
    """
    value = (args.get("granularity") or GRANULARITY_DAY).strip().lower()
    #
    return value if value in _BUCKET_EXPRS else GRANULARITY_DAY


def bucket_expr(granularity):
    """The group-by expression for an already-validated granularity."""
    return _BUCKET_EXPRS.get(granularity, day_expr)()


def bucket_bounds(bucket_start, granularity):
    """[start, end) for one bucket, so a caller renders a row without inferring a week's or a
    month's length from the label alone.
    """
    if bucket_start is None:
        return None, None
    #
    if granularity == GRANULARITY_WEEK:
        bucket_end = bucket_start + datetime.timedelta(days=7)
    elif granularity == GRANULARITY_MONTH:
        year = bucket_start.year + bucket_start.month // 12
        month = bucket_start.month % 12 + 1
        bucket_end = bucket_start.replace(year=year, month=month)
    else:
        bucket_end = bucket_start + datetime.timedelta(days=1)
    #
    return bucket_start, bucket_end


def cost_usd(nano):
    """Nano-USD to USD. Integer nano-dollars are the stored form so sums cannot drift."""
    return round(int(nano or 0) / NANO, 9)


# Response key -> the nano-USD column holding that component. The write path priced all four
# when the call was made, so a reader sums them and never consults the price catalog: editing a
# model's price changes what the next call costs, not what a past call cost.
COST_SPLIT_COLUMNS = {
    "input_cost": UsageEvent.input_cost_nano_usd,
    "output_cost": UsageEvent.output_cost_nano_usd,
    "cache_read_cost": UsageEvent.cache_read_cost_nano_usd,
    "cache_creation_cost": UsageEvent.cache_creation_cost_nano_usd,
}


def cost_split_sums():
    """The four component sums, labelled '<key>_nano' to match cost_split_usd()."""
    return [
        func.sum(func.coalesce(column, 0)).label(f"{key}_nano")
        for key, column in COST_SPLIT_COLUMNS.items()
    ]


def cost_split_usd(row):
    """Component sums as USD. They add up to the row's total, having been stored that way."""
    return {key: cost_usd(row.get(f"{key}_nano")) for key in COST_SPLIT_COLUMNS}


SUB_NANO_KEYS = ("total_cost", "input_cost", "output_cost", "cache_read_cost", "cache_creation_cost")


def _sub_nano_columns():
    """Cost key -> (nano column, token bucket it prices); None means every bucket."""
    return {
        "total_cost": (UsageEvent.cost_nano_usd, None),
        "input_cost": (UsageEvent.input_cost_nano_usd, UsageEvent.billable_input_tokens),
        "output_cost": (UsageEvent.output_cost_nano_usd, UsageEvent.output_tokens),
        "cache_read_cost": (UsageEvent.cache_read_cost_nano_usd, UsageEvent.cache_read_tokens),
        "cache_creation_cost": (UsageEvent.cache_creation_cost_nano_usd, UsageEvent.cache_creation_tokens),
    }


def below_resolution_sums():
    """Per cost key: priced calls with tokens whose cost was under 1 nano-USD, so stored as 0."""
    priced = func.coalesce(UsageEvent.cost_source, "unpriced") != "unpriced"
    return [
        func.sum(case((and_(
            priced,
            func.coalesce(cost, 0) == 0,
            (total_tokens_expr() if tokens is None else func.coalesce(tokens, 0)) > 0,
        ), 1), else_=0)).label(f"{key}_sub_nano")
        for key, (cost, tokens) in _sub_nano_columns().items()
    ]


def below_resolution(row, prefix=""):
    """{response_key: True} for costs that are non-zero but too small to store."""
    return {
        (key if key == "total_cost" else f"{prefix}{key}"): True
        for key in SUB_NANO_KEYS
        if int(row.get(f"{key}_sub_nano") or 0) > 0
    }


def rounded(value, digits=1):
    """ Helper """
    return round(float(value), digits) if value else 0


def is_system_email(email):
    """ Helper """
    if not email:
        return False
    #
    return (
        email in SYSTEM_USER_EMAILS
        or (
            email.startswith(SYSTEM_USER_EMAIL_PREFIX)
            and email.endswith(SYSTEM_USER_EMAIL_SUFFIX)
        )
    )


def resolve_emails(user_ids):
    """user_id -> email, for the ids a payload is about to render.

    usage_event.user_email is populated going forward but is null on rows written before that,
    so the id is the identity and the email is looked up rather than trusted from the row.
    """
    wanted = [uid for uid in {int(u) for u in user_ids if u is not None} if uid != SYSTEM_USER_ID]
    #
    if not wanted:
        return {}
    #
    resolved = {}
    #
    for user in _get_users(wanted):
        if user.get("id") is not None:
            resolved[int(user["id"])] = user.get("email")
    #
    return resolved


def _get_users(user_ids):
    """The directory entries that resolve; a missing user is skipped, not an error.

    One call for the whole id set: list_users resolves them in a single SQL IN, so a project's
    member count does not turn into one round trip per member. It returns *every* user when the
    id list is falsy, hence the guard. The per-id loop stays only as a fallback for a directory
    that does not expose the batched RPC.
    """
    from tools import auth  # pylint: disable=C0415,E0401
    #
    wanted = [user_id for user_id in user_ids if user_id is not None]
    #
    if not wanted:
        return []
    #
    try:
        return [user for user in (auth.list_users(user_ids=wanted) or []) if user]
    except:  # pylint: disable=W0702
        log.warning("usage: batched user lookup unavailable, resolving one at a time")
    #
    users = []
    #
    for user_id in wanted:
        try:
            user = auth.get_user(user_id=user_id)
        except:  # pylint: disable=W0702
            continue
        #
        if user:
            users.append(user)
    #
    return users


def search_user_ids(project_id, search):
    """Project member ids whose email contains search.

    Matching only the stored user_email column would hide every row written before the write
    path populated it, so the search also resolves the project's members through the directory
    and matches there. A member list is small, so the filtering happens in Python.
    """
    from tools import auth  # pylint: disable=C0415,E0401
    #
    needle = (search or "").strip().lower()
    #
    if not needle:
        return []
    #
    try:
        member_ids = auth.list_project_users(project_id) or []
    except:  # pylint: disable=W0702
        log.exception("usage: project member lookup failed for project %s", project_id)
        return []
    #
    matched = []
    #
    for user in _get_users(member_ids):
        email = user.get("email") or ""
        #
        if user.get("id") is not None and needle in email.lower():
            matched.append(int(user["id"]))
    #
    return matched


def resolve_role_filter(project_id, roles):
    """user_id set for the given project role names, or None for "no filter" (today's
    behaviour, all roles).

    One bulk RPC pair for the whole project, never one lookup per role or per member — the
    same shape admin/rpc/roles.py's get_users_roles_in_project already uses. A role that
    exists but has no members returns an empty set, which is a legitimate zero, not a reason
    to fall back to matching everyone.

    Reflects project_user_role as it stands *right now*: a role change or removal rewrites or
    deletes that row, so a row on the far side of that change is no longer in the returned set
    even though its usage_event activity is unchanged (#6796). role_filter_condition is what a
    reader actually wants; this is kept as the live half of that, and for whatever else in the
    plugin still wants today's live-only membership.
    """
    wanted = {role for role in (roles or []) if role}
    #
    if not wanted:
        return None
    #
    from tools import auth  # pylint: disable=C0415,E0401
    #
    try:
        project_roles = auth.list_project_roles(project_id) or []
        user_roles = auth.list_project_user_roles(project_id) or []
    except:  # pylint: disable=W0702
        log.exception("usage: role lookup failed for project %s", project_id)
        return set()
    #
    role_ids = {r["id"] for r in project_roles if r.get("name") in wanted}
    #
    return {ur["user_id"] for ur in user_roles if ur.get("role_id") in role_ids}


# Role names snapshot cache, batched per project (#6796):
# {project_id: (stamp, {user_id: [names]}, ttl_seconds)}.
# usage_event is written far more often than a project's roles change, so a fresh lookup happens
# on cache expiry rather than once per drained batch.
ROLE_SNAPSHOT_TTL_SECONDS = 60
# Failed lookups (e.g. auth outage) are cached too, but briefly -- long enough to spare the
# drainer's hot path from a synchronous RPC per row, short enough to notice auth recovering.
ROLE_SNAPSHOT_FAILURE_TTL_SECONDS = 30
ROLE_SNAPSHOT_CACHE_MAX = 512
_role_snapshot_cache = {}


def fetch_project_role_name_map(project_id):
    """{user_id: sorted role names} for every current member of project_id, uncached; raises
    on a failed lookup so a caller that must not mistake an outage for "no roles" can tell.
    """
    from tools import auth  # pylint: disable=C0415,E0401
    #
    project_roles = auth.list_project_roles(project_id) or []
    user_roles = auth.list_project_user_roles(project_id) or []
    #
    names_by_role_id = {r["id"]: r["name"] for r in project_roles if r.get("name")}
    mapping = {}
    #
    for user_role in user_roles:
        name = names_by_role_id.get(user_role.get("role_id"))
        user_id = user_role.get("user_id")
        #
        if name is None or user_id is None:
            continue
        #
        mapping.setdefault(int(user_id), set()).add(name)
    #
    return {user_id: sorted(names) for user_id, names in mapping.items()}


def _project_role_name_map(project_id):
    """{user_id: sorted role names} for every member of project_id, cached briefly.

    Silent on any failure, including auth being unavailable at all: an unresolvable project
    leaves every row of the batch with no snapshot, which is exactly today's (pre-#6796)
    behaviour for that row -- role_filter_condition falls back to the live join for it. This is
    the write path, so it never logs; a lookup that fails on every call would otherwise log on
    every call.

    Failures are cached too (briefly, per ROLE_SNAPSHOT_FAILURE_TTL_SECONDS): during an auth
    outage, drainer.py's event_values() calls this once per drained row, and without negative
    caching every row for the same project would retry the RPC synchronously.
    """
    now = time.monotonic()
    cached = _role_snapshot_cache.get(project_id)
    #
    if cached is not None and now - cached[0] < cached[2]:
        return cached[1]
    #
    try:
        result = fetch_project_role_name_map(project_id)
    except Exception:  # pylint: disable=W0703
        if len(_role_snapshot_cache) >= ROLE_SNAPSHOT_CACHE_MAX:
            _evict_expired_role_snapshots(now)
        #
        _role_snapshot_cache[project_id] = (now, {}, ROLE_SNAPSHOT_FAILURE_TTL_SECONDS)
        #
        return {}
    #
    if len(_role_snapshot_cache) >= ROLE_SNAPSHOT_CACHE_MAX:
        _evict_expired_role_snapshots(now)
    #
    _role_snapshot_cache[project_id] = (now, result, ROLE_SNAPSHOT_TTL_SECONDS)
    #
    return result


def _evict_expired_role_snapshots(now):
    """Prune only expired entries from the role-snapshot cache (mirrors _is_super_admin's
    eviction), never a blanket clear() -- that would wipe other projects' still-fresh entries
    and trigger a synchronous auth RPC thundering-herd in the drainer's hot path.

    If pruning expired entries still leaves the cache at or over the cap, fall back to evicting
    the oldest entries (by stamp) until it fits, rather than leaving it unbounded.
    """
    for key, entry in list(_role_snapshot_cache.items()):
        if now - entry[0] >= entry[2]:
            _role_snapshot_cache.pop(key, None)
    #
    if len(_role_snapshot_cache) >= ROLE_SNAPSHOT_CACHE_MAX:
        oldest_first = sorted(_role_snapshot_cache.items(), key=lambda item: item[1][0])
        #
        for key, _entry in oldest_first[:len(oldest_first) - ROLE_SNAPSHOT_CACHE_MAX + 1]:
            _role_snapshot_cache.pop(key, None)


def role_names_snapshot(project_id, user_id):
    """This actor's current project role names, or None (unknown, none, or lookup failed).

    Stamped onto a usage_event row at write time -- the one choke point every insert passes
    through regardless of caller (methods/drainer.py event_values()) -- so that a later role
    change or removal cannot make this row's activity disappear from a role-filtered Activity
    trend (#6796). role_filter_condition is what reads it back.
    """
    if project_id is None or user_id is None:
        return None
    #
    try:
        names = _project_role_name_map(int(project_id)).get(int(user_id))
    except (TypeError, ValueError):
        return None
    #
    return names or None


def role_filter_condition(project_id, roles):
    """SQL condition scoping usage_event to rows whose actor held one of `roles`, or None for
    "no filter" (today's behaviour, all roles).

    Prefers each row's own role_snapshot over resolve_role_filter's live join (#6796): the live
    join only ever reflects a user's *current* roles, so once a role changes or a user is
    removed from the project, their already-recorded activity permanently vanished from any
    role-filtered view even though the usage_event row itself was never touched. A row written
    before role_snapshot existed has it NULL and falls back to the live join -- exactly today's
    behaviour for every row already on disk, so nothing regresses for historical data.
    """
    wanted = sorted({role for role in (roles or []) if role})
    #
    if not wanted:
        return None
    #
    live_user_ids = resolve_role_filter(project_id, wanted)
    #
    return or_(
        and_(UsageEvent.role_snapshot.isnot(None), UsageEvent.role_snapshot.overlap(wanted)),
        and_(UsageEvent.role_snapshot.is_(None), UsageEvent.user_id.in_(live_user_ids)),
    )


def label_users(rows_user_ids, stored_emails=None):
    """Display email per user id, preferring the directory over whatever the row stored."""
    resolved = resolve_emails(rows_user_ids)
    stored = stored_emails or {}
    #
    labels = {}
    #
    for user_id in rows_user_ids:
        if user_id is None:
            continue
        #
        key = int(user_id)
        labels[key] = resolved.get(key) or stored.get(key)
    #
    return labels


def _is_super_admin(user_id):
    """Whether this user holds super_admin platform-wide.

    Cached because there is no batched administration-mode roles RPC to ask for the whole set at
    once, and global admin status changes far more slowly than a dashboard reloads. A failed
    lookup is not cached and counts as "not a super-admin", so a transient auth error keeps a
    member in the denominator rather than silently dropping them.
    """
    from tools import rpc_tools  # pylint: disable=C0415,E0401
    #
    now = time.monotonic()
    cached = _super_admin_cache.get(user_id)
    #
    if cached is not None and now - cached[0] < SUPER_ADMIN_TTL_SECONDS:
        return cached[1]
    #
    try:
        roles = rpc_tools.RpcMixin().rpc.timeout(5).auth_get_user_roles(
            user_id, "administration",
        )
    except:  # pylint: disable=W0702
        return False
    #
    verdict = "super_admin" in (roles or [])
    #
    if len(_super_admin_cache) >= SUPER_ADMIN_CACHE_MAX:
        for key, entry in list(_super_admin_cache.items()):
            if now - entry[0] >= SUPER_ADMIN_TTL_SECONDS:
                _super_admin_cache.pop(key, None)
    #
    _super_admin_cache[user_id] = (now, verdict)
    #
    return verdict


def project_member_count(project_id, unique_users=0):
    """Project members, as the adoption denominator.

    Counts members who never made a call, drops the synthetic actors, and drops global
    super-admins — they hold an admin role on every project for oversight rather than as team
    members. A super-admin with real activity still surfaces through the unique_users floor.
    """
    from tools import auth  # pylint: disable=C0415,E0401
    #
    total = 0
    #
    try:
        member_ids = auth.list_project_users(project_id)
        #
        if member_ids:
            humans = [
                user for user in _get_users(member_ids)
                if user.get("email") and not is_system_email(user["email"])
            ]
            #
            total = len([
                user for user in humans
                if not _is_super_admin(user["id"])
            ])
    except:  # pylint: disable=W0702
        log.exception("usage: project member lookup failed for project %s", project_id)
        total = 0
    #
    # A member removed after the fact still has activity in the period, so the denominator
    # must never fall below the numerator
    return max(total, unique_users or 0)


def outsider_admin_ids(project_id, conditions):
    """Active user ids in the window who are global super-admins but not project members.

    A super-admin can act in any project for oversight; that is not team adoption, so they are
    kept off per-user listings. A member removed after the fact is not an outsider here — their
    activity in the window is real and stays listed. A failed member lookup excludes no one.
    """
    from tools import auth  # pylint: disable=C0415,E0401
    #
    try:
        member_ids = {int(uid) for uid in (auth.list_project_users(project_id) or [])}
    except:  # pylint: disable=W0702
        log.exception("usage: project member lookup failed for project %s", project_id)
        return set()
    #
    active_ids = {
        int(r["user_id"]) for r in fetch_all(
            select(distinct(UsageEvent.user_id).label("user_id")).where(*conditions)
        ) if r["user_id"] is not None
    }
    #
    return {
        user_id for user_id in active_ids - member_ids
        if _is_super_admin(user_id)
    }


def model_display_names(project_id):
    """model_name -> display name, from the project's configured models."""
    from tools import rpc_tools  # pylint: disable=C0415,E0401
    #
    names = {}
    #
    try:
        response = rpc_tools.RpcMixin().rpc.timeout(5).configurations_get_models(
            project_id=project_id, section="llm", include_shared=True,
        )
        #
        for item in (response or {}).get("items", []) or []:
            if isinstance(item, dict) and item.get("name"):
                names[item["name"]] = item.get("display_name") or item["name"]
    except:  # pylint: disable=W0702
        log.warning("usage: model display names unavailable for project %s", project_id)
    #
    return names


def fetch_all(statement):
    """Rows as mappings. Core, not ORM: nothing here needs identity or lazy loading."""
    from tools import db  # pylint: disable=C0415,E0401
    #
    with db.engine.connect() as connection:
        return list(connection.execute(statement).mappings())


def fetch_one(statement):
    """ Helper """
    rows = fetch_all(statement)
    #
    return rows[0] if rows else None


def clamp_int(value, default, minimum=None, maximum=None):
    """ Helper """
    try:
        result = int(value)
    except (ValueError, TypeError):
        return default
    #
    if minimum is not None:
        result = max(result, minimum)
    #
    if maximum is not None:
        result = min(result, maximum)
    #
    return result


def select_from(columns, conditions):
    """ Helper """
    return select(*columns).where(*conditions)


def list_available_roles(project_id):
    """Role names defined on the project, for the filter dropdown — independent of any role
    the caller has already selected.
    """
    from tools import auth  # pylint: disable=C0415,E0401
    #
    try:
        project_roles = auth.list_project_roles(project_id) or []
    except:  # pylint: disable=W0702
        log.exception("usage: role lookup failed for project %s", project_id)
        return []
    #
    return sorted({
        r["name"] for r in project_roles
        if r.get("name") and r["name"] not in RESTRICTED_ROLES
    })


def ai_active_users_trend(
        project_id, dt_from=None, dt_to=None, granularity=GRANULARITY_DAY, roles=None, run_scope=None,
):
    """Distinct active vs AI-active users per calendar bucket, optionally restricted to project
    roles.

    Every usage_event row is a metered LLM or tool call (D1's comment on _kpis), so within this
    table active_users and ai_active_users are the same set by construction — both come from
    active_users_expr(), not two different filters. The one implementation behind both the REST
    endpoint and the RPC elitea_core calls to put this number next to its own generic
    active-users count (#5110), so the two can never disagree about a bucket's boundaries or its
    total.
    """
    if run_scope is None:
        dt_from, dt_to = clamp_date_range(dt_from, dt_to)
    granularity = granularity if granularity in _BUCKET_EXPRS else GRANULARITY_DAY
    wanted_roles = sorted({role for role in (roles or []) if role})
    #
    role_condition = role_filter_condition(project_id, wanted_roles)
    conditions = base_filters(project_id, dt_from, dt_to, run_scope=run_scope)
    #
    if role_condition is not None:
        conditions.append(role_condition)
    #
    bucket = bucket_expr(granularity).label("bucket")
    active_users = active_users_expr()
    rows = fetch_all(
        select_from(
            [bucket, active_users.label("active_users"), active_users.label("ai_active_users")],
            conditions,
        ).group_by(bucket).order_by(bucket)
    )
    #
    buckets = []
    for row in rows:
        bucket_start, bucket_end = bucket_bounds(row["bucket"], granularity)
        buckets.append({
            "bucket_start": bucket_start.isoformat() if bucket_start else None,
            "bucket_end": bucket_end.isoformat() if bucket_end else None,
            "active_users": int(row["active_users"] or 0),
            "ai_active_users": int(row["ai_active_users"] or 0),
        })
    #
    return {
        "granularity": granularity,
        "roles": wanted_roles,
        "available_roles": list_available_roles(project_id),
        "buckets": buckets,
    }


def event_type_health(project_id, dt_from=None, dt_to=None, run_scope=None):
    """Per event_type totals/errors/latency for llm and tool, on the same rows Overview counts."""
    if run_scope is None:
        dt_from, dt_to = clamp_date_range(dt_from, dt_to)
    rows = fetch_all(
        select_from(
            [
                UsageEvent.event_type,
                func.count().label("total"),
                count_where(UsageEvent.is_error.is_(True)).label("errors"),
                func.avg(UsageEvent.duration_ms).label("avg_duration_ms"),
            ],
            base_filters(project_id, dt_from, dt_to, run_scope=run_scope),
        ).group_by(UsageEvent.event_type)
    )
    return [
        {
            "event_type": row["event_type"],
            "total": int(row["total"] or 0),
            "errors": int(row["errors"] or 0),
            "avg_duration_ms": float(row["avg_duration_ms"]) if row["avg_duration_ms"] is not None else None,
        }
        for row in rows
    ]

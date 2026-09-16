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

from sqlalchemy import Date, and_, case, cast, distinct, func, select

from pylon.core.tools import log  # pylint: disable=E0611,E0401

from ..models.usage_event import UsageEvent

DEFAULT_DATE_RANGE_DAYS = 7
# One row per calendar day, so an unbounded span is unbounded cardinality
MAX_DATE_RANGE_DAYS = 366

# A platform-initiated call is recorded under a synthetic actor with no email. It is not a
# project member and must never reach a leaderboard or an adoption denominator.
SYSTEM_USER_ID = 0

EVENT_LLM = "llm"
EVENT_TOOL = "tool"

# What the user launched. entity_* is the node that made one call; root_entity_* is the run it
# belongs to, so a run's agent identity survives on every llm and tool row underneath it.
AGENT_ROOT_TYPES = ("application", "pipeline")

# Only a leaderboard row's own actor matters here — the usage_event project_id already scopes
# the query, so there is no user_email allow-list to maintain beyond the synthetic actor.
SYSTEM_USER_EMAILS = ("system@centry.user",)
SYSTEM_USER_EMAIL_PREFIX = "system_user_"
SYSTEM_USER_EMAIL_SUFFIX = "@centry.user"

# Drops the synthetic actor from anything user-facing
HUMAN_ACTOR = UsageEvent.user_id != SYSTEM_USER_ID

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


def parse_date_range(args):
    """[from, to) bounds from date_from/date_to, defaulted and clamped, always aware UTC.

    A single supplied bound anchors the other rather than widening to everything.
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


def base_filters(project_id, dt_from, dt_to, human_only=True):
    """Project and date conditions, leading with (project_id, ts) so the index applies."""
    conditions = [UsageEvent.project_id == project_id]
    #
    if dt_from is not None:
        conditions.append(UsageEvent.ts >= dt_from)
    #
    if dt_to is not None:
        conditions.append(UsageEvent.ts <= dt_to)
    #
    if human_only:
        conditions.append(HUMAN_ACTOR)
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


def billable_input_expr():
    """Input tokens to price at the full input rate.

    Under the inclusive cache convention (OpenAI, Google) cache_read_tokens is part of
    input_tokens, so pricing input_tokens at the full rate *and* cache_read_tokens at the cache
    rate charges a cached token twice. billable_input_tokens is that convention normalised away,
    and it is exactly what the write path prices into cost_micro_usd — so a split built on it
    reconciles with the authoritative total instead of exceeding it.
    """
    return func.coalesce(UsageEvent.billable_input_tokens, 0)


def count_where(condition):
    """Rows matching condition, as a sum over the same single scan."""
    return func.sum(case((condition, 1), else_=0))


def llm_calls_expr():
    """ Helper """
    return count_where(UsageEvent.event_type == EVENT_LLM)


def tool_runs_expr():
    """ Helper """
    return count_where(UsageEvent.event_type == EVENT_TOOL)


def agent_runs_expr():
    """Distinct runs, not the calls inside them.

    One agent run makes many llm and tool calls; summing rows reported each of them as a
    separate run and inflated the figure by whatever the agent's fan-out happened to be.
    """
    return func.count(distinct(case(
        (UsageEvent.root_entity_type.in_(AGENT_ROOT_TYPES), UsageEvent.run_id),
        else_=None,
    )))


def agent_error_runs_expr():
    """Runs that contained at least one failed call.

    Pairs with agent_runs_expr: counting failed calls against a run count would let an
    error_rate exceed 100% whenever one run failed repeatedly.
    """
    return func.count(distinct(case(
        (
            and_(
                UsageEvent.root_entity_type.in_(AGENT_ROOT_TYPES),
                UsageEvent.is_error.is_(True),
            ),
            UsageEvent.run_id,
        ),
        else_=None,
    )))


def is_agent_row():
    """ Helper """
    return UsageEvent.root_entity_type.in_(AGENT_ROOT_TYPES)


def active_users_expr():
    """Distinct human actors."""
    return func.count(distinct(case((HUMAN_ACTOR, UsageEvent.user_id), else_=None)))


def day_expr():
    """ Helper """
    return cast(UsageEvent.ts, Date)


def cost_usd(micro):
    """Micro-USD to USD. Integer micro-dollars are the stored form so sums cannot drift."""
    return round(int(micro or 0) / 1_000_000, 6)


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

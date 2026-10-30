import calendar
import datetime
import re
import sys

from flask import request
from pylon.core.tools import log
from sqlalchemy import func, select
from tools import api_tools, auth, config as c, register_openapi, rpc_tools

PROMPT_LIB_MODE = "prompt_lib"

OPENAPI_TAG = "usage/usage"

SCOPE_PROJECT = "project"
SCOPE_USER = "user"

# Budgets are monthly, so there is no date range to document: the period is implied by
# "now" and reported back in period_start / period_end.
SCOPE_PARAM = {
    "name": "scope",
    "in": "query",
    "required": False,
    "schema": {"type": "string", "enum": [SCOPE_PROJECT, SCOPE_USER], "default": SCOPE_PROJECT},
    "description": (
        "Whose usage to report. 'project' covers the whole project; 'user' covers only "
        "the calling member's own spend within it."
    ),
    "example": SCOPE_PROJECT,
}

# Amount fields stripped for members who may not see platform cost figures
AMOUNT_FIELDS = ("spend", "monthly_limit", "effective_limit", "remaining", "currency")

# Used when the runtime plugin cannot be reached; matches its own default
DEFAULT_WARNING_PCT = 80


def _strip_model_prefixes(model: str):
    """Model id as the configuration registry stores it.

    LiteLLM reports the resolved name, which carries a provider prefix and, for a
    shared model, the owning project id. Neither is part of the configured name.
    """
    without_provider = model.rsplit("/", 1)[-1]
    #
    return re.sub(r"^\d+_", "", without_provider)


def _period_event_where(project_id: int, start, end, user_id=None):
    """WHERE clauses scoping usage_event to the budget window for Usage-page analytics."""
    from ...models.usage_event import UsageEvent  # pylint: disable=C0415
    from ...methods._counters import EVENT_TYPE_LLM  # pylint: disable=C0415
    #
    clauses = [
        UsageEvent.project_id == int(project_id),
        UsageEvent.ts >= start,
        UsageEvent.ts < end,
        UsageEvent.event_type == EVENT_TYPE_LLM,
        UsageEvent.budget_exempt.isnot(True),
    ]
    if user_id is not None:
        clauses.append(UsageEvent.user_id == int(user_id))
    return clauses



def _usage_top_agents(project_id: int, start, end, user_id=None, limit: int = 5, sort_by: str = "spend"):
    """Top N agents/pipelines by spend or runs for the period."""
    from ...models.usage_event import UsageEvent  # pylint: disable=C0415
    from ...methods._analytics import (  # pylint: disable=C0415
        AGENT_ROOT_TYPES, ENTITY_TYPE_EVALUATION, cost_usd, fetch_all,
        root_project_expr, run_name_expr,
    )
    #
    clauses = _period_event_where(project_id, start, end, user_id)
    agent_conditions = [
        *clauses,
        UsageEvent.root_entity_type.in_(AGENT_ROOT_TYPES),
        UsageEvent.root_entity_id.isnot(None),
        func.coalesce(UsageEvent.entity_type, "") != ENTITY_TYPE_EVALUATION,
    ]
    #
    order_col = (
        func.count(func.distinct(UsageEvent.run_id)).desc()
        if sort_by == "runs"
        else func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).desc()
    )
    stmt = (
        select(
            UsageEvent.root_entity_type.label("type"),
            UsageEvent.root_entity_id.label("id"),
            root_project_expr().label("root_project_id"),
            run_name_expr().label("name"),
            func.count(func.distinct(UsageEvent.run_id)).label("runs"),
            func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
        )
        .where(*agent_conditions)
        .group_by(UsageEvent.root_entity_type, UsageEvent.root_entity_id, root_project_expr())
        .order_by(order_col)
        .limit(limit)
    )
    #
    rows = fetch_all(stmt)
    #
    return [
        {
            "type": row["type"],
            "id": row["id"],
            "root_project_id": row["root_project_id"],
            "name": row["name"] or f"#{row['id']}",
            "runs": int(row["runs"] or 0),
            "spend": cost_usd(row["cost_nano"]),
        }
        for row in rows
    ]


def _usage_top_skills(project_id: int, start, end, user_id=None, limit: int = 5, sort_by: str = "spend"):
    """Top N skills by spend or runs for the period."""
    from ...models.usage_event import UsageEvent  # pylint: disable=C0415
    from ...methods._analytics import (  # pylint: disable=C0415
        ENTITY_TYPE_SKILL, cost_usd, fetch_all, root_project_expr, run_name_expr,
    )
    #
    clauses = _period_event_where(project_id, start, end, user_id)
    skill_conditions = [
        *clauses,
        UsageEvent.root_entity_type == ENTITY_TYPE_SKILL,
        UsageEvent.root_entity_id.isnot(None),
    ]
    #
    order_col = (
        func.count(func.distinct(UsageEvent.run_id)).desc()
        if sort_by == "runs"
        else func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).desc()
    )
    stmt = (
        select(
            UsageEvent.root_entity_id.label("id"),
            root_project_expr().label("root_project_id"),
            run_name_expr().label("name"),
            func.count(func.distinct(UsageEvent.run_id)).label("uses"),
            func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
        )
        .where(*skill_conditions)
        .group_by(UsageEvent.root_entity_id, root_project_expr())
        .order_by(order_col)
        .limit(limit)
    )
    #
    rows = fetch_all(stmt)
    #
    return [
        {
            "id": row["id"],
            "root_project_id": row["root_project_id"],
            "name": row["name"] or f"#{row['id']}",
            "uses": int(row["uses"] or 0),
            "spend": cost_usd(row["cost_nano"]),
        }
        for row in rows
    ]


def _available_periods(project_id: int, user_id=None):
    """YYYYMM strings for all months that have counter data, newest-first.

    Always includes the current month even if no spend has been recorded yet, so a new
    project always has something to show in the picker. Limited to 24 months.
    """
    from ...methods._counters import (  # pylint: disable=C0415
        ALL_MODELS_SENTINEL, PERIOD_MONTH, PROJECT_USER_SENTINEL,
    )
    from ...models.usage_counter import UsageCounter  # pylint: disable=C0415
    from ...methods._analytics import fetch_all  # pylint: disable=C0415
    #
    counter_user = int(user_id) if user_id is not None else PROJECT_USER_SENTINEL
    stmt = (
        select(UsageCounter.period_start)
        .where(
            UsageCounter.period_kind == PERIOD_MONTH,
            UsageCounter.project_id == int(project_id),
            UsageCounter.user_id == counter_user,
            UsageCounter.model_name == ALL_MODELS_SENTINEL,
        )
        .order_by(UsageCounter.period_start.desc())
        .limit(24)
    )
    rows = fetch_all(stmt)
    periods = [row["period_start"].strftime("%Y%m") for row in rows if row["period_start"]]
    current = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m")
    if current not in periods:
        periods.insert(0, current)
    return periods


def _usage_monthly_history(project_id: int, user_id=None, n_months: int = 4, anchor_period: str = None):
    """Last N completed months of spend before anchor_period, oldest-first.

    anchor_period defaults to the current month. Uses a single IN-query instead of N round-trips.
    """
    from ...methods._counters import (  # pylint: disable=C0415
        ALL_MODELS_SENTINEL, NANO, PERIOD_MONTH, PROJECT_USER_SENTINEL, period_start,
    )
    from ...models.usage_counter import UsageCounter  # pylint: disable=C0415
    from ...methods._analytics import fetch_all  # pylint: disable=C0415
    #
    if anchor_period:
        try:
            anchor_year, anchor_month = int(anchor_period[:4]), int(anchor_period[4:6])
        except (TypeError, ValueError, IndexError):
            anchor_period = None
    if not anchor_period:
        now = datetime.datetime.now(datetime.timezone.utc)
        anchor_year, anchor_month = now.year, now.month
    #
    months = []
    for delta in range(n_months, 0, -1):
        year = anchor_year + (anchor_month - delta - 1) // 12
        month = (anchor_month - delta - 1) % 12 + 1
        months.append((f"{year:04d}{month:02d}", period_start(
            datetime.date(year, month, 1), PERIOD_MONTH,
        )))
    #
    counter_user = int(user_id) if user_id is not None else PROJECT_USER_SENTINEL
    period_days = [pday for _, pday in months]
    #
    stmt = (
        select(
            UsageCounter.period_start,
            func.sum(UsageCounter.cost_nano_usd).label("cost_nano"),
        )
        .where(
            UsageCounter.period_kind == PERIOD_MONTH,
            UsageCounter.period_start.in_(period_days),
            UsageCounter.project_id == int(project_id),
            UsageCounter.user_id == counter_user,
            UsageCounter.model_name == ALL_MODELS_SENTINEL,
        )
        .group_by(UsageCounter.period_start)
    )
    rows = fetch_all(stmt)
    nano_by_day = {row["period_start"]: int(row["cost_nano"] or 0) for row in rows}
    #
    return [
        {"period": label, "spend": round(nano_by_day.get(pday, 0) / NANO, 9)}
        for label, pday in months
    ]


def _model_display_names(project_id: int):
    """Configured model labels for the usage-by-model table.

    The analytics lookup itself, so one model reads identically in both.
    Empty labels are a safe outcome: callers keep the raw model name.
    """
    from ...methods import _analytics as an  # pylint: disable=C0415
    #
    return an.model_labels(project_id)


def _attach_display_names(models: list, display_names: dict):
    """Add display_name to each usage row that resolves to a configured model.

    Tries the reported name first so an exact registration always wins, then the
    normalised form. Rows that resolve to nothing are left alone rather than given
    the raw id, so the client can fall back to its own formatting.
    """
    if not display_names:
        return models
    #
    for row in models:
        raw = row.get("model") or ""
        display = display_names.get(raw) or display_names.get(_strip_model_prefixes(raw))
        #
        if display:
            row["display_name"] = display
    #
    return models


def _period_reset(period: str):
    """Start of the next monthly period, when the budget tag rolls over."""
    try:
        year, month = int(period[:4]), int(period[4:6])
    except (TypeError, ValueError, IndexError):
        now = datetime.datetime.now(datetime.timezone.utc)
        year, month = now.year, now.month
    #
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    #
    return datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc).isoformat()


def _period_bounds(period: str):
    """First and last day of the period, so the UI can plot a full month."""
    try:
        year, month = int(period[:4]), int(period[4:6])
    except (TypeError, ValueError, IndexError):
        now = datetime.datetime.now(datetime.timezone.utc)
        year, month = now.year, now.month
    #
    last_day = calendar.monthrange(year, month)[1]
    #
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def _is_personal_project(project_id: int, user_id: int):
    """True when this project is the caller's own personal project."""
    try:
        personal_id = rpc_tools.RpcMixin().rpc.timeout(5).projects_get_personal_project_id(
            user_id=user_id,
        )
    except Exception:  # pylint: disable=W0703
        return False
    #
    return personal_id is not None and int(personal_id) == int(project_id)


def _can_see_amounts(project_id: int, user_id: int, is_personal: bool):
    """Members see percentages only; project admins and personal-project owners see cost.

    A personal project's spend is the owner's own, and it is where token-based
    integrations land, so amounts are always shown there.
    """
    if is_personal:
        return True
    #
    try:
        return bool(rpc_tools.RpcMixin().rpc.timeout(5).admin_check_user_is_admin(
            project_id, user_id,
        ))
    except Exception:  # pylint: disable=W0703
        return False


def _redact(payload: dict):
    """Drop cost figures, keeping percentages and token counts."""
    for field in AMOUNT_FIELDS:
        payload.pop(field, None)
    #
    for row in payload.get("models") or []:
        row.pop("spend", None)
    #
    for row in payload.get("daily") or []:
        row.pop("spend", None)
        row.pop("by_model", None)
        row.pop("by_source", None)
    #
    for row in payload.get("tool_spend") or []:
        for field in AMOUNT_FIELDS:
            row.pop(field, None)
    for row in payload.get("by_source") or []:
        row.pop("spend", None)
    #
    for row in payload.get("top_agents") or []:
        row.pop("spend", None)
    #
    for row in payload.get("top_agents_by_runs") or []:
        row.pop("spend", None)
    #
    for row in payload.get("top_skills") or []:
        row.pop("spend", None)
    #
    for row in payload.get("top_skills_by_runs") or []:
        row.pop("spend", None)
    #
    for row in payload.get("monthly_history") or []:
        row.pop("spend", None)
    #
    token_mix = payload.get("token_mix") or {}
    for key in list(token_mix):
        if key.endswith("_cost"):
            token_mix.pop(key, None)
    #
    return payload


def _feature_row(feature: dict, spend: dict, scope: str):
    """One paid feature's Usage row; a failed spend read is marked, never shown as $0."""
    limit = feature.get("limit_usd")
    row = {
        "feature": feature["feature"],
        "label": feature.get("label") or feature["feature"],
        "effective_limit": limit,
        "limit_source": feature.get("limit_source", "unlimited"),
        "enabled": feature.get("enabled", True),
        "currency": "USD",
        "spend_available": bool(spend.get("available")),
    }
    if not row["spend_available"]:
        return row
    member = scope == SCOPE_USER
    spent = int(spend.get("member_spent_nano" if member else "project_spent_nano") or 0) / 1_000_000_000
    row.update({
        "spend": spent,
        "calls": int(spend.get("member_calls" if member else "project_calls") or 0),
        "remaining": None if limit is None else max(0.0, limit - spent),
        "percent_used": None if not limit else round(spent / limit * 100, 2),
    })
    return row


def _tool_spend(module, project_id: int, user_id: int, scope: str, period: str = None):
    """Paid tools (e.g. web search): limits from elitea_core, spend off usage's own counters."""
    member_id = user_id if scope == SCOPE_USER else None
    try:
        features = rpc_tools.RpcMixin().rpc.timeout(10).elitea_core_get_tool_spend_limits(
            project_id=project_id, user_id=member_id,
        ) or []
    except Exception:  # pylint: disable=W0703
        log.warning("usage: tool spend limits unavailable for project %s: %s", project_id, sys.exc_info()[1])
        return []
    # When a historical period is requested, pass a moment at its start so the counter
    # lookup reads that month's data rather than the current month.
    moment = None
    if period:
        from ...methods.reconcile import period_bounds as _pb  # pylint: disable=C0415
        moment = _pb(period)[0]
    return [
        _feature_row(feature, module.usage_read_feature_spend(project_id, member_id, feature["feature"], moment=moment), scope)
        for feature in features
    ]


def _warning_pct(scope: str, is_personal: bool):
    """Configured percent-of-limit at which this scope warns."""
    threshold_scope = "user" if scope == SCOPE_USER else (
        "personal_project" if is_personal else "project"
    )
    #
    try:
        return rpc_tools.RpcMixin().rpc.timeout(5).usage_get_warning_threshold(
            scope=threshold_scope,
        )
    except Exception:  # pylint: disable=W0703
        return DEFAULT_WARNING_PCT


def _member_default(rpc, project_id: int):
    """The project's member default, for the path where the resolver is unreachable."""
    try:
        budget = rpc.timeout(5).elitea_core_get_project_budget(project_id=project_id) or {}
        return budget.get("member_default_limit")
    except Exception:  # pylint: disable=W0703
        return None


def _usage_daily_breakdown(project_id: int, start, end, user_id=None):
    """Per-day spend and tokens split by model and source bucket.

    Runs two GROUP BY queries (day × model, day × source) and returns a flat list
    of breakdown rows that _attach_daily_breakdown folds into the daily array.
    """
    from ...models.usage_event import UsageEvent  # pylint: disable=C0415
    from ...methods._analytics import (  # pylint: disable=C0415
        cost_usd, fetch_all, source_bucket_expr, total_tokens_expr,
    )
    #
    clauses = _period_event_where(project_id, start, end, user_id)
    day = func.date_trunc("day", UsageEvent.ts)
    source = source_bucket_expr()
    breakdown = []
    for dimension, column in (
        ("model", func.coalesce(UsageEvent.model_name, "")),
        ("source", source),
    ):
        stmt = (
            select(
                day.label("day"),
                column.label("key"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
                func.sum(total_tokens_expr()).label("tokens"),
            )
            .where(*clauses)
            .group_by(day, column)
        )
        breakdown.extend(
            {
                "date": row["day"].date().isoformat(),
                "dimension": dimension,
                "key": row["key"],
                "spend": cost_usd(row["cost_nano"]),
                "total_tokens": int(row["tokens"] or 0),
            }
            for row in fetch_all(stmt)
        )
    return breakdown


def _attach_daily_breakdown(daily: list, breakdown: list):
    """Fold breakdown rows into each daily entry as by_<dimension> spend/token dicts."""
    for day_row in daily:
        day_row["by_model"] = {}
        day_row["by_model_tokens"] = {}
        day_row["by_source"] = {}
        day_row["by_source_tokens"] = {}
    #
    rows_by_date = {day_row["date"]: day_row for day_row in daily}
    for row in breakdown:
        day_row = rows_by_date.get(row["date"])
        if day_row is None:
            continue
        dim = row["dimension"]
        day_row[f"by_{dim}"][row["key"]] = row["spend"]
        day_row[f"by_{dim}_tokens"][row["key"]] = row["total_tokens"]
    return daily


def _usage_state(project_id: int, user_id: int, scope: str, is_personal: bool, period: str = None):
    """Budget state plus the per-model and per-day breakdown for one scope."""
    rpc = rpc_tools.RpcMixin().rpc
    #
    if scope == SCOPE_USER:
        budget = rpc.timeout(5).elitea_core_get_user_budget(
            project_id=project_id, user_id=user_id,
        ) or {}
        #
        try:
            # RPC serialization can stringify the key; a missed hit reads as unlimited
            limits = rpc.timeout(10).elitea_core_get_effective_member_limits(
                project_id=project_id, user_ids=[user_id],
            ) or {}
            limit = limits.get(user_id, limits.get(str(user_id)))
        except Exception:  # pylint: disable=W0703
            stored = budget.get("monthly_limit") if budget.get("enabled", True) else None
            limit = stored if stored is not None else _member_default(rpc, project_id)
        #
        try:
            detail = rpc.timeout(30).usage_get_user_usage_detail(
                project_id=project_id, user_id=user_id, period=period,
            ) or {}
        except Exception:  # pylint: disable=W0703
            log.warning("usage: user detail unavailable for project %s user %s: %s",
                        project_id, user_id, sys.exc_info()[1])
            detail = {}
    else:
        budget = rpc.timeout(5).elitea_core_get_project_budget(project_id=project_id) or {}
        #
        try:
            limit = rpc.timeout(5).elitea_core_get_effective_project_limit(project_id=project_id)
        except Exception:  # pylint: disable=W0703
            limit = budget.get("monthly_limit") if budget.get("enabled", True) else None
        #
        try:
            detail = rpc.timeout(30).usage_get_project_usage_detail(
                project_id=project_id, period=period,
            ) or {}
        except Exception:  # pylint: disable=W0703
            log.warning("usage: project detail unavailable for project %s: %s",
                        project_id, sys.exc_info()[1])
            detail = {}
    #
    spent = float(detail.get("spend", 0) or 0)
    period = detail.get("period") or f"{datetime.datetime.now(datetime.timezone.utc):%Y%m}"
    period_start, period_end = _period_bounds(period)
    #
    from ...methods.reconcile import period_bounds as _event_period_bounds  # pylint: disable=C0415
    ev_start, ev_end = _event_period_bounds(period)
    uid_for_query = user_id if scope == SCOPE_USER else None
    #
    # New analytics fields — each query degrades gracefully on exception
    try:
        top_agents = _usage_top_agents(project_id, ev_start, ev_end, uid_for_query, sort_by="spend")
    except Exception:  # pylint: disable=W0703
        log.warning("usage: top_agents unavailable for project %s: %s",
                    project_id, sys.exc_info()[1])
        top_agents = []
    #
    try:
        top_agents_by_runs = _usage_top_agents(project_id, ev_start, ev_end, uid_for_query, sort_by="runs")
    except Exception:  # pylint: disable=W0703
        log.warning("usage: top_agents_by_runs unavailable for project %s: %s",
                    project_id, sys.exc_info()[1])
        top_agents_by_runs = []
    #
    try:
        top_skills = _usage_top_skills(project_id, ev_start, ev_end, uid_for_query, sort_by="spend")
    except Exception:  # pylint: disable=W0703
        log.warning("usage: top_skills unavailable for project %s: %s",
                    project_id, sys.exc_info()[1])
        top_skills = []
    #
    try:
        top_skills_by_runs = _usage_top_skills(project_id, ev_start, ev_end, uid_for_query, sort_by="runs")
    except Exception:  # pylint: disable=W0703
        log.warning("usage: top_skills_by_runs unavailable for project %s: %s",
                    project_id, sys.exc_info()[1])
        top_skills_by_runs = []
    #
    try:
        monthly_history = _usage_monthly_history(
            project_id, uid_for_query, anchor_period=period,
        )
    except Exception:  # pylint: disable=W0703
        log.warning("usage: monthly_history unavailable for project %s: %s",
                    project_id, sys.exc_info()[1])
        monthly_history = []
    #
    try:
        available_periods = _available_periods(project_id, uid_for_query)
    except Exception:  # pylint: disable=W0703
        log.warning("usage: available_periods unavailable for project %s: %s",
                    project_id, sys.exc_info()[1])
        available_periods = []
    #
    # Token counts from event detail
    input_tokens = int(detail.get("input_tokens", 0) or 0)
    output_tokens = int(detail.get("output_tokens", 0) or 0)
    cache_read_tokens = int(detail.get("cache_read_tokens", 0) or 0)
    cache_creation_tokens = int(detail.get("cache_creation_tokens", 0) or 0)
    #
    # Per-component costs: stored per event, so they are exact regardless of token-type prices.
    # detail may carry these when usage_event_detail is updated to include cost_split_sums();
    # until then they are absent and fall back to 0.
    token_mix = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cache_read_tokens,
        "input_tokens_cost": detail.get("input_cost", 0) or 0,
        "output_tokens_cost": detail.get("output_cost", 0) or 0,
        "cached_tokens_cost": detail.get("cache_read_cost", 0) or 0,
    }
    # cache_hit_rate: fraction of billable input served from cache (0–100).
    # billable_input_tokens excludes cache reads, so cache_read / billable_input is the right ratio.
    billable_input = int(detail.get("billable_input_tokens", input_tokens) or 0)
    cache_hit_rate = round(cache_read_tokens / billable_input * 100, 2) if billable_input else None
    #
    # by_source comes from usage_event_detail (via spend.py) and is already in detail
    by_source = detail.get("by_source") or []
    #
    # Per-day model and source breakdown — attach in-place to each daily row
    daily = detail.get("daily") or []
    try:
        breakdown = _usage_daily_breakdown(project_id, ev_start, ev_end, uid_for_query)
        _attach_daily_breakdown(daily, breakdown)
    except Exception:  # pylint: disable=W0702
        log.warning("usage: daily breakdown unavailable for project %s: %s",
                    project_id, sys.exc_info()[1])
    #
    return {
        "project_id": project_id,
        "user_id": user_id if scope == SCOPE_USER else None,
        "scope": scope,
        "monthly_limit": budget.get("monthly_limit"),
        "effective_limit": limit,
        # An exempt row's stored limit does not apply, so it must not read as explicit
        "limit_source": "explicit" if (
            budget.get("monthly_limit") is not None and budget.get("enabled", True)
        ) else ("default" if limit is not None else "unlimited"),
        "currency": budget.get("currency", "USD"),
        "spend": spent,
        "remaining": None if limit is None else max(0.0, limit - spent),
        "percent_used": None if not limit else round(spent / limit * 100, 2),
        "warning_pct": _warning_pct(scope, is_personal),
        "total_tokens": detail.get("total_tokens", 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "api_requests": detail.get("api_requests", 0),
        "models": _attach_display_names(
            detail.get("models") or [], _model_display_names(project_id),
        ),
        "daily": daily,
        "period": period,
        "period_start": period_start,
        "period_end": period_end,
        "resets_at": _period_reset(period),
        "spend_available": detail.get("available", False),
        # New v2 fields
        "by_source": by_source,
        "top_agents": top_agents,
        "top_agents_by_runs": top_agents_by_runs,
        "top_skills": top_skills,
        "top_skills_by_runs": top_skills_by_runs,
        "token_mix": token_mix,
        "cache_hit_rate": cache_hit_rate,
        "monthly_history": monthly_history,
        "available_periods": available_periods,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


class PromptLibAPI(api_tools.APIModeHandler):
    """A project member reads usage for the project or for themselves."""

    @register_openapi(
        name="Get Project Usage",
        description=(
            "Current-period spend against the applicable budget, with the per-day and "
            "per-model breakdown behind Settings -> Usage. Only calls to platform-shared "
            "models are counted; models configured with a project's own credentials are "
            "billed by that provider and never appear here.\n\n"
            "The period is the current month and is reported back in period_start, "
            "period_end and resets_at, so no date range is accepted.\n\n"
            "Members who may not see platform cost figures receive the same payload with "
            "spend, limits and currency removed and can_see_amounts set to false; "
            "percentages and token counts are still present.\n\n"
            "tool_spend lists paid tool features (e.g. web search) with their own spend, "
            "limit and call count; they never count toward the LLM figures."
        ),
        tags=[OPENAPI_TAG],
        parameters=[
            {
                "name": "project_id",
                "in": "path",
                "required": True,
                "schema": {"type": "integer"},
                "description": "Project to report on.",
                "example": 1,
            },
            SCOPE_PARAM,
        ],
        responses={
            "200": {"description": "Usage and budget state for the requested scope."},
            "400": {"description": "scope was neither 'project' nor 'user'."},
        },
    )
    @auth.decorators.check_api(
        {
            "permissions": ["models.project_context.view"],
            "recommended_roles": {
                c.DEFAULT_MODE: {"admin": True, "editor": True, "viewer": True},
            },
        }
    )
    @api_tools.endpoint_metrics
    def get(self, project_id: int, **kwargs):
        user_id = auth.current_user().get("id")
        #
        scope = request.args.get("scope", SCOPE_PROJECT)
        #
        if scope not in (SCOPE_PROJECT, SCOPE_USER):
            return {"error": "scope must be 'project' or 'user'"}, 400
        #
        period = request.args.get("period") or None
        if period:
            if not re.match(r"^\d{6}$", period):
                return {"error": "period must be in YYYYMM format"}, 400
            month = int(period[4:6])
            if not 1 <= month <= 12:
                return {"error": "period month must be 01–12"}, 400
            current = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m")
            if period > current:
                return {"error": "period must not be in the future"}, 400
        #
        is_personal = _is_personal_project(project_id, user_id)
        #
        payload = _usage_state(project_id, user_id, scope, is_personal, period=period)
        payload["tool_spend"] = _tool_spend(self.module, project_id, user_id, scope, period=period)
        #
        visible = _can_see_amounts(project_id, user_id, is_personal)
        payload["can_see_amounts"] = visible
        #
        return (payload if visible else _redact(payload)), 200


class API(api_tools.APIBase):
    url_params = api_tools.with_modes(
        [
            "<int:project_id>/usage",
        ]
    )

    mode_handlers = {
        PROMPT_LIB_MODE: PromptLibAPI,
    }

"""
Paginated analytics users endpoint, ported onto usage_event (#6574).

Provides server-side pagination, search, and sorting for user activity data.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import asc, desc, distinct, func, literal, or_, select

    from ...methods import _analytics as an
    from ...models.usage_event import UsageEvent

    try:
        from plugins.costs.models.model_price import ModelPrice
        _model_price_available = True
    except ImportError:
        ModelPrice = None
        _model_price_available = False

    _SORT_WHITELIST = frozenset([
        "total_events", "active_days", "llm_events", "tool_events",
        "agent_events", "chat_events", "errors", "user_email",
        "total_tokens", "llm_cost",
    ])

    class PromptLibAPI(api_tools.APIModeHandler):
        """Paginated user activity for analytics."""

        @register_openapi(
            name="List User Analytics",
            description=(
                "Returns paginated user activity statistics broken down by LLM calls, "
                "tool runs, agent interactions, and chat events, with sorting and search."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need a paginated leaderboard or searchable list of user activity across a project. Do not use this tool when you need the detailed model/tool/agent breakdown for one individual — use Get User Analytics Detail. Do not use for project-level KPI dashboards. This is the primary list/discovery endpoint for user analytics.",
            tags=["usage/analytics"],
            parameters=[
                {
                    "name": "project_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer"},
                    "description": "Project ID.",
                    "example": 1,
                },
                {
                    "name": "date_from",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "format": "date-time"},
                    "description": "Start datetime (ISO 8601). Defaults to 7 days ago.",
                    "example": "2025-01-01T00:00:00",
                },
                {
                    "name": "date_to",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "format": "date-time"},
                    "description": "End datetime (ISO 8601). Defaults to now.",
                    "example": "2025-01-31T23:59:59",
                },
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                    "description": "Page size (max 100).",
                },
                {
                    "name": "offset",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "default": 0, "minimum": 0},
                    "description": "Pagination offset.",
                },
                {
                    "name": "search",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Filter by user email (case-insensitive partial match).",
                },
                {
                    "name": "sort_by",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "type": "string",
                        "enum": [
                            "total_events", "active_days", "llm_events",
                            "tool_events", "agent_events", "chat_events",
                            "errors", "user_email", "total_tokens", "llm_cost",
                        ],
                        "default": "total_events",
                    },
                    "description": "Column to sort by.",
                },
                {
                    "name": "sort_order",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    "description": "Sort direction.",
                },
            ],
            responses={
                "200": {
                    "description": "Paginated user analytics",
                    "content": {
                        "application/json": {
                            "example": {
                                "total": 18,
                                "rows": [
                                    {
                                        "user_id": 42,
                                        "user_email": "alice@example.com",
                                        "total_events": 320,
                                        "active_days": 14,
                                        "llm_events": 200,
                                        "tool_events": 90,
                                        "agent_events": 60,
                                        "chat_events": 0,
                                        "errors": 5,
                                        "total_tokens": 84500,
                                        "input_tokens": 60000,
                                        "output_tokens": 20000,
                                        "cache_read_tokens": 3000,
                                        "cache_creation_tokens": 1500,
                                        "llm_cost": 0.00845,
                                        "input_cost": 0.006,
                                        "output_cost": 0.002,
                                        "cache_read_cost": 0.0003,
                                        "cache_creation_cost": 0.0002,
                                    },
                                ],
                            }
                        }
                    },
                },
                "401": {"description": "Unauthorized"},
                "500": {"description": "Internal server error"},
            },
            available_to_users=True,
        )
        @auth.decorators.check_api({
            "permissions": ["models.monitoring.tracing.view"],
            "recommended_roles": {
                c.DEFAULT_MODE: {"admin": True, "editor": True, "viewer": True},
            }
        })
        @api_tools.endpoint_metrics
        def get(self, project_id: int, **kwargs):
            """
            GET /api/v2/usage/analytics_users/prompt_lib/<project_id>

            Query params:
                date_from, date_to: ISO date range
                limit (int): page size, default 20, max 100
                offset (int): pagination offset, default 0
                search (str): filter by email (ILIKE)
                sort_by (str): column to sort, default "total_events"
                sort_order (str): "asc" or "desc", default "desc"
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)

                limit = an.clamp_int(request.args.get("limit"), 20, maximum=100)
                offset = an.clamp_int(request.args.get("offset"), 0, minimum=0)

                sort_by = request.args.get("sort_by", "total_events")
                if sort_by not in _SORT_WHITELIST:
                    sort_by = "total_events"
                sort_order = request.args.get("sort_order", "desc")
                search = request.args.get("search", "").strip()

                conditions = an.base_filters(project_id, dt_from, dt_to)
                if search:
                    # The stored email is null on rows written before the write path filled it,
                    # so the directory's matching ids are searched alongside the column
                    search_filter = UsageEvent.user_email.ilike(f"%{search}%")
                    matched_ids = an.search_user_ids(project_id, search)
                    if matched_ids:
                        search_filter = or_(search_filter, UsageEvent.user_id.in_(matched_ids))
                    conditions.append(search_filter)

                total_row = an.fetch_one(select(
                    func.count(distinct(UsageEvent.user_id)).label("total"),
                ).where(*conditions))
                total = int(total_row["total"] or 0) if total_row else 0

                # Aggregate columns, one scan per user
                email_col = func.max(UsageEvent.user_email).label("user_email")
                total_events_col = func.count().label("total_events")
                active_days_col = func.count(distinct(an.day_expr())).label("active_days")
                llm_col = an.llm_calls_expr().label("llm_events")
                tool_col = an.tool_runs_expr().label("tool_events")
                # D4 fix: distinct run_id, not a row count — an agent run makes many calls
                agent_col = an.agent_runs_expr().label("agent_events")
                errors_col = an.count_where(UsageEvent.is_error.is_(True)).label("errors")
                # D1 fix: no is_error zeroing — a provider that charged for an errored call
                # is still owed that charge
                total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
                input_tokens_col = func.sum(
                    func.coalesce(UsageEvent.input_tokens, 0),
                ).label("input_tokens")
                output_tokens_col = func.sum(
                    func.coalesce(UsageEvent.output_tokens, 0),
                ).label("output_tokens")
                cache_read_tokens_col = func.sum(
                    func.coalesce(UsageEvent.cache_read_tokens, 0),
                ).label("cache_read_tokens")
                cache_creation_tokens_col = func.sum(
                    func.coalesce(UsageEvent.cache_creation_tokens, 0),
                ).label("cache_creation_tokens")
                cost_micro_col = func.sum(
                    func.coalesce(UsageEvent.cost_micro_usd, 0),
                ).label("cost_micro")

                extra_cost_cols = []
                if _model_price_available:
                    input_cost_col = func.sum(
                        func.coalesce(UsageEvent.input_tokens, 0)
                        * func.coalesce(ModelPrice.input_cost_per_token, 0),
                    ).label("input_cost")
                    output_cost_col = func.sum(
                        func.coalesce(UsageEvent.output_tokens, 0)
                        * func.coalesce(ModelPrice.output_cost_per_token, 0),
                    ).label("output_cost")
                    cache_read_cost_col = func.sum(
                        func.coalesce(UsageEvent.cache_read_tokens, 0)
                        * func.coalesce(ModelPrice.cache_read_input_token_cost, 0),
                    ).label("cache_read_cost")
                    cache_creation_cost_col = func.sum(
                        func.coalesce(UsageEvent.cache_creation_tokens, 0)
                        * func.coalesce(ModelPrice.cache_creation_input_token_cost, 0),
                    ).label("cache_creation_cost")
                    extra_cost_cols = [
                        input_cost_col, output_cost_col,
                        cache_read_cost_col, cache_creation_cost_col,
                    ]

                statement = select(
                    UsageEvent.user_id,
                    email_col,
                    total_events_col,
                    active_days_col,
                    llm_col,
                    tool_col,
                    agent_col,
                    errors_col,
                    total_tokens_col,
                    input_tokens_col,
                    output_tokens_col,
                    cache_read_tokens_col,
                    cache_creation_tokens_col,
                    cost_micro_col,
                    *extra_cost_cols,
                ).where(*conditions).group_by(UsageEvent.user_id)

                if _model_price_available:
                    statement = statement.outerjoin(
                        ModelPrice, UsageEvent.model_name == ModelPrice.model_name,
                    )

                sort_map = {
                    "total_events": total_events_col,
                    "active_days": active_days_col,
                    "llm_events": llm_col,
                    "tool_events": tool_col,
                    "agent_events": agent_col,
                    # chat_events has no usage_event equivalent; unsortable, kept in the
                    # whitelist only so a client passing it does not fall through to an error
                    "chat_events": literal(0),
                    "errors": errors_col,
                    "user_email": email_col,
                    "total_tokens": total_tokens_col,
                    "llm_cost": cost_micro_col,
                }
                col = sort_map.get(sort_by, total_events_col)
                order_fn = desc if sort_order == "desc" else asc
                statement = statement.order_by(order_fn(col)).offset(offset).limit(limit)

                rows = an.fetch_all(statement)

                emails = an.label_users(
                    [r["user_id"] for r in rows],
                    {r["user_id"]: r["user_email"] for r in rows if r["user_id"] is not None},
                )

                return {
                    "total": total,
                    "rows": [
                        {
                            "user_id": r["user_id"],
                            "user_email": emails.get(r["user_id"]),
                            "total_events": r["total_events"],
                            "active_days": r["active_days"],
                            "llm_events": int(r["llm_events"] or 0),
                            "tool_events": int(r["tool_events"] or 0),
                            "agent_events": int(r["agent_events"] or 0),
                            # No socketio-style chat event exists in usage_event; the UI
                            # renders this column, so the key stays, always zero
                            "chat_events": 0,
                            "errors": int(r["errors"] or 0),
                            "total_tokens": int(r["total_tokens"] or 0),
                            "input_tokens": int(r["input_tokens"] or 0),
                            "output_tokens": int(r["output_tokens"] or 0),
                            "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                            "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                            "llm_cost": an.cost_usd(r["cost_micro"]),
                            "input_cost": round(float(r["input_cost"]), 6)
                            if _model_price_available and r["input_cost"] else 0.0,
                            "output_cost": round(float(r["output_cost"]), 6)
                            if _model_price_available and r["output_cost"] else 0.0,
                            "cache_read_cost": round(float(r["cache_read_cost"]), 6)
                            if _model_price_available and r["cache_read_cost"] else 0.0,
                            "cache_creation_cost": round(float(r["cache_creation_cost"]), 6)
                            if _model_price_available and r["cache_creation_cost"] else 0.0,
                        }
                        for r in rows
                    ],
                }, 200

            except Exception:
                log.error("Analytics users query failed", exc_info=True)
                return {"error": "Failed to query analytics users"}, 500


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

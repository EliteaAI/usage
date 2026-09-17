"""
Paginated analytics agents endpoint (usage_event port of elitea_core's analytics_agents.py).

An agent's identity lives on root_entity_id/root_entity_type: it is the run every llm/tool call
under it belongs to, not the individual call itself. Rows are therefore grouped by
root_entity_id, never entity_id.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import asc, case, desc, func, select

    from ...methods import _analytics as an
    from ...models.usage_event import UsageEvent

    _SORT_WHITELIST = frozenset([
        "events", "users", "avg_duration_ms", "errors", "entity_name",
        "total_tokens", "llm_cost",
    ])

    class PromptLibAPI(api_tools.APIModeHandler):
        """Paginated agent/pipeline usage for analytics."""

        @register_openapi(
            name="List Agent Analytics",
            description=(
                "Returns paginated agent/pipeline usage statistics with optional "
                "date filtering, search by name, sorting, and a daily chat-message trend."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need a leaderboard or paginated comparison of agents/pipelines in a project, with search and sorting. Do not use this tool when you need the full breakdown for one specific agent — use Get Agent Analytics Detail. Do not use for overall project KPIs — use Get Project AI Analytics. This is the primary discovery/list endpoint for agent-level analytics.",
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
                    "description": "Filter by agent/pipeline name (case-insensitive partial match).",
                },
                {
                    "name": "sort_by",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "type": "string",
                        "enum": ["events", "users", "avg_duration_ms", "errors", "entity_name", "total_tokens", "llm_cost"],
                        "default": "events",
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
                    "description": "Paginated agent analytics",
                    "content": {
                        "application/json": {
                            "example": {
                                "total": 5,
                                "rows": [
                                    {
                                        "entity_name": "Code Review Bot",
                                        "entity_id": 7,
                                        "events": 95,
                                        "users": 5,
                                        "avg_duration_ms": 1200.0,
                                        "errors": 4,
                                        "total_tokens": 84500,
                                        "llm_cost": 0.00845,
                                    },
                                ],
                                "chat_daily": [],
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
            GET /api/v2/usage/analytics_agents/prompt_lib/<project_id>
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = an.base_filters(project_id, dt_from, dt_to) + [
                    an.is_agent_row(),
                    UsageEvent.root_entity_id.isnot(None),
                ]

                limit = an.clamp_int(request.args.get("limit"), 20, minimum=1, maximum=100)
                offset = an.clamp_int(request.args.get("offset"), 0, minimum=0)

                sort_by = request.args.get("sort_by", "events")
                if sort_by not in _SORT_WHITELIST:
                    sort_by = "events"
                sort_order = request.args.get("sort_order", "desc")
                search = request.args.get("search", "").strip()

                total, rows = self._agents(conditions, search, sort_by, sort_order, limit, offset)

                return {
                    "total": total,
                    "rows": rows,
                    # socketio "chat_predict" activity has no usage_event equivalent; kept
                    # empty so the UI's chat chart section (which hides on an empty list)
                    # stays intact.
                    "chat_daily": [],
                }, 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics agents query failed", exc_info=True)
                return {"error": "Failed to query analytics agents"}, 500

        @staticmethod
        def _agents(conditions, search, sort_by, sort_order, limit, offset):
            """Grouped-by-run agent rows, paginated and sorted."""
            # There is no root_entity_name column: the display name is read off whichever
            # row IS the run (entity_id == root_entity_id), never guessed from a child call.
            entity_name_expr = func.max(case(
                (UsageEvent.entity_id == UsageEvent.root_entity_id, UsageEvent.entity_name),
                else_=None,
            ))

            events_col = an.agent_runs_expr().label("events")
            users_col = func.count(func.distinct(UsageEvent.user_id)).label("users")
            avg_dur_col = func.avg(UsageEvent.duration_ms).label("avg_duration_ms")
            # Runs, not calls, to stay comparable with events above
            errors_col = an.agent_error_runs_expr().label("errors")
            input_tokens_col = func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens")
            output_tokens_col = func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens")
            cache_read_tokens_col = func.sum(
                func.coalesce(UsageEvent.cache_read_tokens, 0)
            ).label("cache_read_tokens")
            cache_creation_tokens_col = func.sum(
                func.coalesce(UsageEvent.cache_creation_tokens, 0)
            ).label("cache_creation_tokens")
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            # Cost and its split are both meter-time columns on the row: no price-catalog join
            # anywhere here, so a later price edit cannot restate what these runs cost. Also
            # not error-filtered, unlike the audit_events port (D1).
            cost_micro_col = func.sum(func.coalesce(UsageEvent.cost_micro_usd, 0)).label("cost_micro")
            llm_calls_col = an.llm_calls_expr().label("llm_calls")

            columns = [
                UsageEvent.root_entity_id.label("entity_id"),
                entity_name_expr.label("entity_name"),
                events_col, users_col, avg_dur_col, errors_col,
                input_tokens_col, output_tokens_col,
                cache_read_tokens_col, cache_creation_tokens_col,
                total_tokens_col, cost_micro_col, llm_calls_col,
            ] + [
                func.sum(func.coalesce(column, 0)).label(f"{key}_micro")
                for key, column in an.COST_SPLIT_COLUMNS.items()
            ]

            stmt = select(*columns).where(*conditions).group_by(UsageEvent.root_entity_id)
            if search:
                stmt = stmt.having(entity_name_expr.ilike(f"%{search}%"))

            total_row = an.fetch_one(select(func.count().label("total")).select_from(stmt.subquery()))
            total = (total_row["total"] if total_row else 0) or 0

            sort_map = {
                "events": events_col,
                "users": users_col,
                "avg_duration_ms": avg_dur_col,
                "errors": errors_col,
                "entity_name": entity_name_expr,
                "total_tokens": total_tokens_col,
                "llm_cost": cost_micro_col,
            }
            order_fn = desc if sort_order == "desc" else asc
            rows = an.fetch_all(
                stmt.order_by(order_fn(sort_map.get(sort_by, events_col))).limit(limit).offset(offset)
            )

            return total, [
                {
                    "entity_name": r["entity_name"] or f"Agent #{r['entity_id']}",
                    "entity_id": r["entity_id"],
                    "events": int(r["events"] or 0),
                    "users": r["users"] or 0,
                    "avg_duration_ms": an.rounded(r["avg_duration_ms"], 1),
                    "errors": int(r["errors"] or 0),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(r["cost_micro"]),
                    **{key: an.cost_usd(r[f"{key}_micro"]) for key in an.COST_SPLIT_COLUMNS},
                    # int() on both operands: sum() over bigint columns comes back as Decimal,
                    # and a single Decimal anywhere in the payload makes the whole response
                    # unserializable — the endpoint 500s instead of rendering any row.
                    "avg_tokens_per_call": (
                        round(int(r["total_tokens"] or 0) / int(r["llm_calls"]), 1)
                        if r["llm_calls"] else 0
                    ),
                }
                for r in rows
            ]


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

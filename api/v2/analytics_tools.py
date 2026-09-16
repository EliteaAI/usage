"""
Paginated analytics tools endpoint, ported onto usage_event (#6574).

Tool call rows are identified by tool_name IS NOT NULL; usage_event carries a partial index
on exactly that predicate (project_id, tool_name, ts), so filters lead with project_id/ts.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import asc, desc, distinct, func, select

    from ...methods import _analytics as an
    from ...models.usage_event import UsageEvent

    _SORT_WHITELIST = frozenset([
        "calls", "users", "avg_duration_ms", "errors", "tool_name",
    ])

    class PromptLibAPI(api_tools.APIModeHandler):
        """Paginated tool usage for analytics."""

        @register_openapi(
            name="List Tool Analytics",
            description=(
                "Returns paginated tool usage statistics with optional date filtering, "
                "search by tool name, and sorting."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need a project-wide ranking or paginated inventory of tool usage, including call volume and error counts. Do not use this tool when you need the detailed per-user/per-agent breakdown for one exact tool — use Get Tool Analytics Detail. Do not use for project dashboard KPIs. This is the main discovery/list endpoint for tool analytics.",
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
                    "description": "Filter by tool name (case-insensitive partial match).",
                },
                {
                    "name": "sort_by",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "type": "string",
                        "enum": ["calls", "users", "avg_duration_ms", "errors", "tool_name"],
                        "default": "calls",
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
                    "description": "Paginated tool analytics",
                    "content": {
                        "application/json": {
                            "example": {
                                "total": 12,
                                "rows": [
                                    {
                                        "tool_name": "jira_create_issue",
                                        "calls": 120,
                                        "users": 6,
                                        "avg_duration_ms": 310.0,
                                        "errors": 3,
                                    },
                                    {
                                        "tool_name": "github_create_pr",
                                        "calls": 85,
                                        "users": 4,
                                        "avg_duration_ms": 420.0,
                                        "errors": 1,
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
            GET /api/v2/usage/analytics_tools/prompt_lib/<project_id>

            Query params:
                date_from, date_to: ISO date range
                limit (int): page size, default 20, max 100
                offset (int): pagination offset, default 0
                search (str): filter by tool_name (ILIKE)
                sort_by (str): column to sort, default "calls"
                sort_order (str): "asc" or "desc", default "desc"
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)

                limit = an.clamp_int(request.args.get("limit"), 20, maximum=100)
                offset = an.clamp_int(request.args.get("offset"), 0, minimum=0)

                sort_by = request.args.get("sort_by", "calls")
                if sort_by not in _SORT_WHITELIST:
                    sort_by = "calls"
                sort_order = request.args.get("sort_order", "desc")
                search = request.args.get("search", "").strip()

                conditions = an.base_filters(project_id, dt_from, dt_to) + [
                    UsageEvent.tool_name.isnot(None),
                    UsageEvent.tool_name != "",
                ]
                if search:
                    conditions.append(UsageEvent.tool_name.ilike(f"%{search}%"))

                calls_col = func.count().label("calls")
                users_col = func.count(distinct(UsageEvent.user_id)).label("users")
                avg_dur_col = func.avg(UsageEvent.duration_ms).label("avg_duration_ms")
                errors_col = an.count_where(UsageEvent.is_error.is_(True)).label("errors")

                total_row = an.fetch_one(select(
                    func.count(distinct(UsageEvent.tool_name)).label("total"),
                ).where(*conditions))
                total = int(total_row["total"] or 0) if total_row else 0

                sort_map = {
                    "calls": calls_col,
                    "users": users_col,
                    "avg_duration_ms": avg_dur_col,
                    "errors": errors_col,
                    "tool_name": UsageEvent.tool_name,
                }
                order_fn = desc if sort_order == "desc" else asc

                rows = an.fetch_all(select(
                    UsageEvent.tool_name,
                    calls_col,
                    users_col,
                    avg_dur_col,
                    errors_col,
                ).where(*conditions).group_by(
                    UsageEvent.tool_name,
                ).order_by(
                    order_fn(sort_map.get(sort_by, calls_col)),
                ).offset(offset).limit(limit))

                return {
                    "total": total,
                    "rows": [
                        {
                            "tool_name": r["tool_name"],
                            "calls": r["calls"],
                            "users": r["users"],
                            "avg_duration_ms": an.rounded(r["avg_duration_ms"], 1),
                            "errors": r["errors"] or 0,
                        }
                        for r in rows
                    ],
                }, 200

            except Exception:
                log.error("Analytics tools query failed", exc_info=True)
                return {"error": "Failed to query analytics tools"}, 500


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

"""
AI-active-users trend, bucketed by day/week/month and optionally filtered by project role
(#5110). A distinct new report shape — not a query DSL over analytics.py's existing daily
series — because it adds two dimensions (granularity, role) that endpoint does not have.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request

    from ...methods import _analytics as an

    class PromptLibAPI(api_tools.APIModeHandler):
        """AI-active-users activity trend, bucketed and role-filterable."""

        @register_openapi(
            name="Get AI Activity Trend",
            description=(
                "Returns a bucketed trend of distinct AI-active users (project members who "
                "made at least one metered LLM or tool call) for a date range, grouped by "
                "calendar day, week, or month, and optionally restricted to one or more "
                "project roles."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need a day/week/month trend of how many distinct users were AI-active in a project, optionally scoped to one or more roles. Do not use this tool for a paginated per-user or per-agent leaderboard — use List User Analytics or List Agent Analytics instead. Do not use for generic (non-AI) activity, which this endpoint does not report.",
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
                    "example": "2026-01-01T00:00:00",
                },
                {
                    "name": "date_to",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "format": "date-time"},
                    "description": "End datetime (ISO 8601). Defaults to now.",
                    "example": "2026-05-31T23:59:59",
                },
                {
                    "name": "granularity",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "type": "string",
                        "enum": ["day", "week", "month"],
                        "default": "day",
                    },
                    "description": (
                        "Bucket size. Weeks and months are calendar-aligned (Postgres "
                        "date_trunc: weeks start Monday). An unrecognised value defaults to day."
                    ),
                },
                {
                    "name": "roles",
                    "in": "query",
                    "required": False,
                    "style": "form",
                    "explode": True,
                    "schema": {"type": "array", "items": {"type": "string"}},
                    "description": (
                        "Zero or more project role names to filter by (repeat the parameter "
                        "for multiple values, e.g. roles=Viewer&roles=Editor; a single "
                        "comma-separated value is also accepted). Omitted or empty means all "
                        "roles — today's behaviour. A role with no members legitimately "
                        "returns all-zero buckets rather than an unfiltered query."
                    ),
                },
            ],
            responses={
                "200": {
                    "description": "Bucketed AI-active-users trend",
                    "content": {
                        "application/json": {
                            "example": {
                                "granularity": "week",
                                "roles": ["Viewer"],
                                "buckets": [
                                    {
                                        "bucket_start": "2026-01-05T00:00:00+00:00",
                                        "bucket_end": "2026-01-12T00:00:00+00:00",
                                        "ai_active_users": 4,
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
            GET /api/v2/usage/analytics_activity/prompt_lib/<project_id>

            Query params:
                date_from, date_to: ISO date range
                granularity (str): "day" | "week" | "month", default "day"
                roles (list[str]): zero or more project role names; repeatable or
                    comma-separated. Omitted/empty means all roles.
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)

                return an.ai_active_users_trend(
                    project_id, dt_from, dt_to,
                    an.parse_granularity(request.args), self._role_params(),
                ), 200

            except Exception:  # pylint: disable=W0703
                log.error("Analytics activity query failed", exc_info=True)
                return {"error": "Failed to query analytics activity"}, 500

        @staticmethod
        def _role_params():
            """Role names from repeated ?roles=... params, each also split on comma so a
            single comma-separated value works too.
            """
            roles = []
            for raw in request.args.getlist("roles"):
                roles.extend(part.strip() for part in raw.split(",") if part.strip())
            #
            return roles


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

"""
Paginated skills analytics endpoint (#6929): one row per skill, its runs and in-agent uses.
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
    from ...methods import _skill_analytics as sa

    class PromptLibAPI(api_tools.APIModeHandler):
        """Paginated skill usage for analytics."""

        @register_openapi(
            name="List Skill Analytics",
            description=(
                "Returns paginated skill usage statistics: standalone/chat runs (with tokens and "
                "cost) and in-agent uses (activations inside agent runs, no own tokens or cost), "
                "with date filtering, search by name and sorting."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need a leaderboard or paginated comparison of skills in a project: how often each skill ran on its own or in chats, how often agents activated it, who used it and what its runs cost. Do not use this tool for one skill's full breakdown — use Get Skill Analytics Detail. Do not use it for agents — use List Agent Analytics.",
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
                    "description": "Filter by skill name (case-insensitive partial match).",
                },
                {
                    "name": "sort_by",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "enum": list(sa.SORT_COLUMNS), "default": sa.DEFAULT_SORT},
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
                    "description": "Paginated skill analytics",
                    "content": {
                        "application/json": {
                            "example": {
                                "total": 1,
                                "summary": {
                                    "skills_used": 1, "runs": 12, "in_agent_uses": 30,
                                    "total_tokens": 48000, "cost": 0.0123,
                                },
                                "rows": [
                                    {
                                        "skill_id": 7,
                                        "skill_project_id": 2,
                                        "skill_name": "pdf-summarizer",
                                        "deleted": False,
                                        "runs": 12,
                                        "in_agent_uses": 30,
                                        "users": 4,
                                        "agents_using": 3,
                                        "avg_duration_ms": 2100.0,
                                        "errors": 1,
                                        "total_tokens": 48000,
                                        "cost": 0.0123,
                                        "last_used": "2025-01-31T10:00:00+00:00",
                                        "avg_est_body_tokens": 350.0,
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
            GET /api/v2/usage/analytics_skills/prompt_lib/<project_id>
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                sort_by = request.args.get("sort_by", sa.DEFAULT_SORT)
                return sa.build_skill_leaderboard(
                    project_id, dt_from, dt_to,
                    search=request.args.get("search", "").strip(),
                    sort_by=sort_by if sort_by in sa.SORT_COLUMNS else sa.DEFAULT_SORT,
                    sort_order="asc" if request.args.get("sort_order") == "asc" else "desc",
                    limit=an.clamp_int(request.args.get("limit"), 20, minimum=1, maximum=100),
                    offset=an.clamp_int(request.args.get("offset"), 0, minimum=0),
                ), 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics skills query failed", exc_info=True)
                return {"error": "Failed to query analytics skills"}, 500


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

"""
Single-skill analytics detail endpoint (#6929).
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
        """Full breakdown for one skill."""

        @register_openapi(
            name="Get Skill Analytics Detail",
            description=(
                "Returns the usage breakdown for one skill: KPIs, a daily trend, breakdowns by "
                "version, model, user and trigger source, the agents that activated it, the "
                "load_skill vs mention split and its recent runs."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need the full drill-down for one specific skill: its KPIs, daily trend, version/model/user/trigger breakdowns, which agents used it and its recent runs. Do not use this tool for a leaderboard across skills — use List Skill Analytics.",
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
                    "name": "skill_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "integer"},
                    "description": "Skill ID.",
                    "example": 7,
                },
                {
                    "name": "skill_project_id",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer"},
                    "description": "Project the skill lives in, e.g. the public project for a Catalog skill. Defaults to project_id.",
                },
                {
                    "name": "date_from",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "format": "date-time"},
                    "description": "Start datetime (ISO 8601). Defaults to 7 days ago.",
                },
                {
                    "name": "date_to",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "format": "date-time"},
                    "description": "End datetime (ISO 8601). Defaults to now.",
                },
            ],
            responses={
                "200": {"description": "Skill analytics detail"},
                "400": {"description": "skill_id is missing or not an integer"},
                "401": {"description": "Unauthorized"},
                "404": {"description": "No usage of the skill in range"},
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
            GET /api/v2/usage/analytics_skill_detail/prompt_lib/<project_id>
            """
            skill_id = an.clamp_int(request.args.get("skill_id"), None)
            if skill_id is None:
                return {"error": "skill_id is required"}, 400
            skill_project_id = an.clamp_int(request.args.get("skill_project_id"), project_id)
            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                detail = sa.skill_detail(project_id, skill_id, skill_project_id, dt_from, dt_to)
            except Exception:  # pylint: disable=W0703
                log.error("Analytics skill detail query failed", exc_info=True)
                return {"error": "Failed to query analytics skill detail"}, 500
            if detail is None:
                return {"error": "Skill not used in the selected range"}, 404
            return detail, 200


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

"""
Complexity of one agent/pipeline (#7554): rollup summary, daily trend and its scored runs.
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
    from ...methods import _complexity_analytics as ca
    from .analytics_complexity import DATE_PARAMETERS, META_EXAMPLE, with_names

    class PromptLibAPI(api_tools.APIModeHandler):
        """Complexity summary and scored runs of one agent/pipeline."""

        @register_openapi(
            name="Get Agent Complexity Detail",
            description=(
                "Returns one agent/pipeline's complexity for the date range: the same summary as the "
                "list (median/average CU, breakdown, label, percentile), a daily trend, and its scored "
                "runs with per-run CU, label and percentile, paginated and sortable."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need the Complexity Units of one specific agent or pipeline and its individual scored runs. Do not use for ranking agents — use List Agent Complexity.",
            tags=["usage/analytics"],
            parameters=[
                *DATE_PARAMETERS,
                {
                    "name": "entity_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "integer"},
                    "description": "Agent/pipeline root entity ID.",
                },
                {
                    "name": "entity_type",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "enum": list(an.AGENT_ROOT_TYPES), "default": "application"},
                    "description": "Root entity type as stored on the run.",
                },
                {
                    "name": "entity_project_id",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer"},
                    "description": "Project owning the agent (a public agent). Defaults to project_id.",
                },
                {
                    "name": "sort_by",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "enum": sorted(ca.RUN_SORTS), "default": "ts"},
                    "description": "Run column to sort by.",
                },
            ],
            responses={
                "200": {
                    "description": "Agent complexity detail",
                    "content": {"application/json": {"example": {
                        "meta": META_EXAMPLE,
                        "range": {"date_from": "2026-11-09", "date_to": "2026-11-16"},
                        "summary": {"entity_id": 7, "runs": 42, "cu_median": 12.4, "label": "M"},
                        "daily": [{"day": "2026-11-15", "runs": 6, "cu_avg": 14.2, "cu_max": 33.0}],
                        "total": 42,
                        "runs": [{
                            "run_id": "67a08328-…", "started_at": "2026-11-15T10:00:00+00:00",
                            "cu_total": 18.3, "breakdown": {"context": 7.3, "reasoning": 5.0, "tools": 6.0},
                            "label": "M", "percentile": 80, "tool_counts": {"read_file": 4},
                        }],
                    }}},
                },
                "400": {"description": "Missing or invalid entity"},
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
            GET /api/v2/usage/analytics_complexity_detail/prompt_lib/<project_id>
            """
            entity_id = an.clamp_int(request.args.get("entity_id"), None)
            entity_type = request.args.get("entity_type") or "application"
            if entity_id is None or entity_type not in an.AGENT_ROOT_TYPES:
                return {"error": "entity_id and a valid entity_type are required"}, 400
            entity_project_id = an.clamp_int(request.args.get("entity_project_id"), project_id)
            try:
                invalid_range = an.date_range_error(request.args)
                if invalid_range:
                    return invalid_range
                dt_from, dt_to = an.parse_date_range(request.args)
                day_from, day_to = ca.day_bounds(dt_from, dt_to)
                limit = an.clamp_int(request.args.get("limit"), 20, minimum=1, maximum=100)
                offset = an.clamp_int(request.args.get("offset"), 0, minimum=0)
                order = ca.order_clause(
                    ca.RUN_SORTS, request.args.get("sort_by"), request.args.get("sort_order"), "ts",
                )
                #
                state = an.fetch_one(ca.state_statement())
                enabled = self.module.usage_complexity_is_enabled()
                body = {
                    "meta": ca.meta(state, enabled),
                    "range": {"date_from": day_from.isoformat(), "date_to": day_to.isoformat()},
                    "summary": None, "daily": [], "total": 0, "runs": [],
                }
                if not enabled:
                    return body, 200
                #
                key = {
                    "project_id": project_id, "entity_project_id": entity_project_id,
                    "entity_type": entity_type, "entity_id": entity_id,
                }
                summary = an.fetch_all(ca.entity_statement(ca.ENTITY_SORTS["runs"] + " DESC", key=True).bindparams(
                    day_from=day_from, day_to=day_to, limit=1, offset=0, **key,
                ))
                if summary:
                    body["summary"] = with_names(project_id, [ca.entity_row(summary[0], state)])[0]
                body["daily"] = [
                    {"day": r["day"].isoformat(), "runs": int(r["runs"]),
                     "cu_avg": round(float(r["cu_avg"]), 2), "cu_max": round(float(r["cu_max"]), 2)}
                    for r in an.fetch_all(ca.daily_trend_statement().bindparams(
                        day_from=day_from, day_to=day_to, **key,
                    ))
                ]
                runs = an.fetch_all(ca.runs_statement(order).bindparams(
                    dt_from=dt_from, dt_to=dt_to, limit=limit, offset=offset, **key,
                ))
                body["total"] = int(runs[0]["total"]) if runs else 0
                body["runs"] = [ca.run_row(r, state) for r in runs]
                return body, 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics complexity detail query failed", exc_info=True)
                return {"error": "Failed to query agent complexity"}, 500


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

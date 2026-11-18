"""
Agent/pipeline complexity leaderboard (#7554), read from the entity_complexity_daily rollup only.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


DATE_PARAMETERS = [
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
        "description": "Start datetime (ISO 8601), truncated to its UTC day. Defaults to 7 days ago.",
    },
    {
        "name": "date_to",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "format": "date-time"},
        "description": "End datetime (ISO 8601), exclusive. Defaults to now.",
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
        "name": "sort_order",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
        "description": "Sort direction.",
    },
]

META_EXAMPLE = {
    "enabled": True,
    "formula_version": 1,
    "thresholds": {"medium": 10.0, "high": 30.0, "extreme": 100.0},
    "thresholds_source": "default",
    "thresholds_computed_at": "2026-11-16T02:41:00+00:00",
    "as_of": "2026-11-16T09:23:00+00:00",
    "scored_through": "2026-11-16T08:53:00+00:00",
    "stale": False,
}


if _API_AVAILABLE:
    from flask import request

    from ...methods import _analytics as an
    from ...methods import _complexity_analytics as ca

    class PromptLibAPI(api_tools.APIModeHandler):
        """Paginated complexity of the project's agents/pipelines."""

        @register_openapi(
            name="List Agent Complexity",
            description=(
                "Returns per agent/pipeline complexity in Complexity Units (CU) for the date range: "
                "median and average CU per run, the context/reasoning/tools breakdown, the L/M/H/X "
                "label and percentile of the median, run count, and freshness of the scores."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need to rank or compare agents/pipelines in a project by how complex their runs are (Complexity Units, L/M/H/X labels). Do not use for one agent's scored runs — use Get Agent Complexity Detail. Do not use for token/cost leaderboards — use List Agent Analytics.",
            tags=["usage/analytics"],
            parameters=[
                *DATE_PARAMETERS,
                {
                    "name": "sort_by",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "enum": sorted(ca.ENTITY_SORTS), "default": "cu_median"},
                    "description": "Column to sort by; percentile ranks by median with unlabelled rows last.",
                },
                {
                    "name": "search",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": (
                        "Case-insensitive substring of the agent/pipeline name. search_truncated is true when "
                        "more names matched than the lookup returns, so some agents may be missing."
                    ),
                },
            ],
            responses={
                "200": {
                    "description": "Paginated agent complexity",
                    "content": {"application/json": {"example": {
                        "meta": META_EXAMPLE,
                        "range": {"date_from": "2026-11-09", "date_to": "2026-11-16"},
                        "total": 1,
                        "search_truncated": False,
                        "rows": [{
                            "entity_id": 7, "entity_project_id": 1, "entity_type": "application",
                            "entity_kind": "agent", "entity_name": "Code Review Bot", "runs": 42,
                            "cu_median": 12.4, "cu_avg": 15.1, "cu_max": 61.0,
                            "breakdown": {"context": 6.2, "reasoning": 4.1, "tools": 4.8},
                            "label": "M", "label_pending": False, "percentile": 71,
                        }],
                    }}},
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
            GET /api/v2/usage/analytics_complexity/prompt_lib/<project_id>
            """
            try:
                invalid_range = an.date_range_error(request.args)
                if invalid_range:
                    return invalid_range
                dt_from, dt_to = an.parse_date_range(request.args)
                day_from, day_to = ca.day_bounds(dt_from, dt_to)
                limit = an.clamp_int(request.args.get("limit"), 20, minimum=1, maximum=100)
                offset = an.clamp_int(request.args.get("offset"), 0, minimum=0)
                order = ca.order_clause(
                    ca.ENTITY_SORTS, request.args.get("sort_by"), request.args.get("sort_order"), "cu_median",
                )
                #
                state = an.fetch_one(ca.state_statement())
                enabled = self.module.usage_complexity_is_enabled()
                body = {
                    "meta": ca.meta(state, enabled),
                    "range": {"date_from": day_from.isoformat(), "date_to": day_to.isoformat()},
                    "total": 0,
                    "search_truncated": False,
                    "rows": [],
                }
                if not enabled:
                    return body, 200
                #
                search = request.args.get("search", "").strip()
                search_params = {}
                if search:
                    matches, body["search_truncated"] = an.application_search(project_id, search)
                    if not matches:
                        return body, 200
                    search_params = ca.search_params(matches)
                rows = an.fetch_all(ca.entity_statement(order, search=bool(search)).bindparams(
                    project_id=project_id, day_from=day_from, day_to=day_to, limit=limit, offset=offset,
                    **search_params,
                ))
                body["total"] = int(rows[0]["total"]) if rows else 0
                body["rows"] = with_names(project_id, [ca.entity_row(r, state) for r in rows])
                return body, 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics complexity query failed", exc_info=True)
                return {"error": "Failed to query agent complexity"}, 500


    def with_names(project_id, rows):
        """Names and agent/pipeline kind from elitea_core, keyed by the owning project."""
        applications = an.application_meta(
            project_id, refs=[(r["entity_project_id"], r["entity_id"]) for r in rows],
        )
        for row in rows:
            key = (row["entity_project_id"], row["entity_id"])
            row["entity_name"] = (applications.get(key) or {}).get("name") or f"Agent #{row['entity_id']}"
            row["entity_kind"] = an.application_kind(applications, *key, default="agent")
        return rows


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

"""
Tool detail analytics endpoint, ported onto usage_event (#6574).

Returns per-tool KPIs, per-user breakdown, associated agents, and a daily usage trend.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import case, distinct, func, select

    from ...methods import _analytics as an
    from ...models.usage_event import UsageEvent

    _AGENT_LIMIT = 20

    class PromptLibAPI(api_tools.APIModeHandler):
        """Per-tool detail analytics."""

        @register_openapi(
            name="Get Tool Analytics Detail",
            description=(
                "Returns KPIs, per-user breakdown, associated agents, and daily usage trend "
                "for a single tool identified by tool_name."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need a full drill-down on one known tool, including who used it, which agents invoked it, and how its usage/errors changed over time. Do not use this tool to browse all tools in a project — use List Tool Analytics first. Do not use if you only know a partial tool name and still need discovery. This endpoint is best for 'investigate this exact tool.'",
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
                    "name": "tool_name",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Exact tool name to inspect.",
                    "example": "jira_create_issue",
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
            ],
            responses={
                "200": {
                    "description": "Tool detail analytics",
                    "content": {
                        "application/json": {
                            "example": {
                                "tool_name": "jira_create_issue",
                                "kpis": {
                                    "total_calls": 120,
                                    "unique_users": 6,
                                    "avg_duration_ms": 310.0,
                                    "errors": 3,
                                    "error_rate": 2.5,
                                },
                                "users": [
                                    {
                                        "user_id": 42,
                                        "user_email": "alice@example.com",
                                        "calls": 55,
                                        "avg_duration_ms": 290.0,
                                        "errors": 1,
                                    }
                                ],
                                "agents": [
                                    {
                                        "entity_name": "Code Review Bot",
                                        "entity_id": 7,
                                        "calls": 45,
                                    }
                                ],
                                "daily_usage": [
                                    {"date": "2025-01-15", "calls": 18, "errors": 0},
                                    {"date": "2025-01-16", "calls": 22, "errors": 1},
                                ],
                            }
                        }
                    },
                },
                "400": {"description": "tool_name is required"},
                "401": {"description": "Unauthorized"},
                "404": {"description": "No data found for this tool"},
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
            GET /api/v2/usage/analytics_tool_detail/prompt_lib/<project_id>

            Query params:
                tool_name (str): required
                date_from, date_to: ISO date range
            """
            tool_name = request.args.get("tool_name")
            if not tool_name:
                return {"error": "tool_name is required"}, 400

            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = an.base_filters(project_id, dt_from, dt_to) + [
                    UsageEvent.tool_name == tool_name,
                ]

                kpi = an.fetch_one(select(
                    func.count().label("total_calls"),
                    func.count(distinct(UsageEvent.user_id)).label("unique_users"),
                    func.avg(UsageEvent.duration_ms).label("avg_duration_ms"),
                    an.count_where(UsageEvent.is_error.is_(True)).label("errors"),
                ).where(*conditions))

                if not kpi or not kpi["total_calls"]:
                    return {"error": "No data found for this tool"}, 404

                total_calls = kpi["total_calls"]
                errors = kpi["errors"] or 0

                return {
                    "tool_name": tool_name,
                    "kpis": {
                        "total_calls": total_calls,
                        "unique_users": kpi["unique_users"],
                        "avg_duration_ms": an.rounded(kpi["avg_duration_ms"], 1),
                        "errors": errors,
                        "error_rate": round(errors / total_calls * 100, 2) if total_calls > 0 else 0,
                    },
                    "users": self._users(conditions),
                    "agents": self._agents(conditions),
                    "daily_usage": self._daily_usage(conditions),
                }, 200

            except Exception:
                log.error("Analytics tool detail query failed", exc_info=True)
                return {"error": "Failed to query tool detail"}, 500

        @staticmethod
        def _users(conditions):
            """Per-user breakdown.

            Grouped by user_id alone: user_email is null on rows written before the write path
            populated it, so grouping by the pair would split one person into several rows.
            """
            rows = an.fetch_all(select(
                UsageEvent.user_id,
                func.max(UsageEvent.user_email).label("user_email"),
                func.count().label("calls"),
                func.avg(UsageEvent.duration_ms).label("avg_duration_ms"),
                an.count_where(UsageEvent.is_error.is_(True)).label("errors"),
            ).where(*conditions, UsageEvent.user_id.isnot(None)).group_by(
                UsageEvent.user_id,
            ).order_by(func.count().desc()))

            emails = an.label_users(
                [r["user_id"] for r in rows],
                {r["user_id"]: r["user_email"] for r in rows if r["user_id"] is not None},
            )

            return [
                {
                    "user_id": r["user_id"],
                    "user_email": emails.get(r["user_id"]),
                    "calls": r["calls"],
                    "avg_duration_ms": an.rounded(r["avg_duration_ms"], 1),
                    "errors": r["errors"] or 0,
                }
                for r in rows
            ]

        @staticmethod
        def _agents(conditions):
            """Agents (applications/pipelines) that ran this tool, by call volume.

            usage_event tags every tool row with the run's own root_entity_type/id directly
            (root defaults to the invoking entity for a single-level agent call), so this reads
            straight off the row instead of the elitea_core original's trace_id join to a
            separate application-type event.

            calls is tool invocations, not runs: this sits inside a tool's detail view, where the
            question is how much of the tool's traffic each agent drove.

            entity_name names the node that made the call, and there is no root_entity_name, so
            it only labels the root when the two are the same node — a nested run would otherwise
            title its parent with a sub-agent's name.
            """
            root_name = func.max(case(
                (UsageEvent.entity_id == UsageEvent.root_entity_id, UsageEvent.entity_name),
                else_=None,
            ))
            #
            rows = an.fetch_all(select(
                UsageEvent.root_entity_type,
                UsageEvent.root_entity_id,
                root_name.label("entity_name"),
                func.count().label("calls"),
            ).where(
                *conditions,
                an.is_agent_row(),
                UsageEvent.root_entity_id.isnot(None),
            ).group_by(
                UsageEvent.root_entity_type,
                UsageEvent.root_entity_id,
            ).order_by(func.count().desc()).limit(_AGENT_LIMIT))

            return [
                {
                    "entity_name": r["entity_name"] or f"Agent #{r['root_entity_id']}",
                    "entity_id": r["root_entity_id"],
                    "calls": r["calls"],
                }
                for r in rows
            ]

        @staticmethod
        def _daily_usage(conditions):
            """ Daily call/error trend """
            day = an.day_expr().label("day")

            rows = an.fetch_all(select(
                day,
                func.count().label("calls"),
                an.count_where(UsageEvent.is_error.is_(True)).label("errors"),
            ).where(*conditions).group_by(day).order_by(day))

            return [
                {
                    "date": r["day"].isoformat() if r["day"] else None,
                    "calls": r["calls"],
                    "errors": r["errors"] or 0,
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

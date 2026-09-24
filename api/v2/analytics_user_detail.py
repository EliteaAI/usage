"""
User detail analytics endpoint, ported onto usage_event (#6574).

Returns per-user KPIs, model/tool/agent breakdown, and daily activity.
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
        """Per-user detail analytics."""

        @register_openapi(
            name="Get User Analytics Detail",
            description=(
                "Returns KPIs, model usage, tool usage, agent usage, and daily activity "
                "breakdown for a single user identified by user_id."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need a drill-down on one known user's activity: which models they used, which tools they invoked, which agents they interacted with, and how active they were by day. Do not use this tool to browse all users — use List User Analytics. Do not use for overall project or agent/tool-centric dashboards. This is the correct endpoint for 'analyze this specific user.'",
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
                    "name": "user_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "integer"},
                    "description": "User ID to inspect.",
                    "example": 42,
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
                    "description": "User detail analytics",
                    "content": {
                        "application/json": {
                            "example": {
                                "user_id": 42,
                                "user_email": "alice@example.com",
                                "kpis": {
                                    "total_events": 320,
                                    "active_days": 14,
                                    "llm_events": 200,
                                    "tool_events": 90,
                                    "agent_events": 60,
                                    "chat_events": 0,
                                    "errors": 5,
                                    "input_tokens": 60000,
                                    "output_tokens": 20000,
                                    "total_tokens": 84500,
                                    "cache_read_tokens": 3000,
                                    "cache_creation_tokens": 1500,
                                    "llm_cost": 0.00845,
                                    "input_cost": 0.006,
                                    "output_cost": 0.002,
                                    "cache_read_cost": 0.0003,
                                    "cache_creation_cost": 0.0002,
                                    "avg_cost_per_call": 0.0000422,
                                },
                                "models": [
                                    {
                                        "model_name": "gpt-4o",
                                        "display_name": "GPT-4o",
                                        "calls": 150,
                                    },
                                ],
                                "tools": [
                                    {"tool_name": "jira_create_issue", "calls": 55},
                                ],
                                "agents": [
                                    {
                                        "entity_name": "Code Review Bot",
                                        "entity_id": 7,
                                        "runs": 40,
                                    }
                                ],
                                "daily_activity": [
                                    {
                                        "date": "2025-01-15",
                                        "llm": 18,
                                        "tool": 8,
                                        "chat": 0,
                                        "agent": 4,
                                        "total": 35,
                                    }
                                ],
                            }
                        }
                    },
                },
                "400": {"description": "user_id is required or invalid"},
                "401": {"description": "Unauthorized"},
                "404": {"description": "No data found for this user"},
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
            GET /api/v2/usage/analytics_user_detail/prompt_lib/<project_id>

            Query params:
                user_id (int): required
                date_from, date_to: ISO date range
            """
            user_id = request.args.get("user_id")
            if not user_id:
                return {"error": "user_id is required"}, 400
            try:
                user_id = int(user_id)
            except (ValueError, TypeError):
                return {"error": "user_id must be an integer"}, 400

            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = an.base_filters(project_id, dt_from, dt_to) + [
                    UsageEvent.user_id == user_id,
                ]

                kpi = self._kpis(conditions)
                if not kpi or not kpi["total_events"]:
                    return {"error": "No data found for this user"}, 404

                return {
                    "user_id": user_id,
                    "user_email": an.label_users([user_id], {user_id: kpi["user_email"]}).get(user_id),
                    "kpis": kpi["kpis"],
                    "models": self._models(project_id, conditions),
                    "tools": self._tools(conditions),
                    "agents": self._agents(conditions),
                    "daily_activity": self._daily_activity(conditions),
                }, 200

            except Exception:
                log.error("Analytics user detail query failed", exc_info=True)
                return {"error": "Failed to query user detail"}, 500

        @staticmethod
        def _kpis(conditions):
            """One scan for every headline number.

            Not grouped: the caller has already pinned this to a single user_id, so grouping
            by user_email as the original did was splitting one person's rows whenever the
            stored email varied (or was null) across them.
            """
            statement = select(
                func.max(UsageEvent.user_email).label("user_email"),
                func.count().label("total_events"),
                func.count(distinct(an.day_expr())).label("active_days"),
                an.llm_calls_expr().label("llm_events"),
                an.tool_runs_expr().label("tool_events"),
                # D4 fix: distinct run_id, not a row count
                an.agent_runs_expr().label("agent_events"),
                an.count_where(UsageEvent.is_error.is_(True)).label("errors"),
                # D1 fix: no is_error filter/zeroing on tokens or cost below — tool-call rows
                # naturally carry zero tokens/cost, so summing across the whole scan is safe
                func.sum(
                    func.coalesce(UsageEvent.input_tokens, 0),
                ).label("input_tokens"),
                func.sum(
                    func.coalesce(UsageEvent.output_tokens, 0),
                ).label("output_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(
                    func.coalesce(UsageEvent.cache_read_tokens, 0),
                ).label("cache_read_tokens"),
                func.sum(
                    func.coalesce(UsageEvent.cache_creation_tokens, 0),
                ).label("cache_creation_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
                *an.cost_split_sums(),
            ).where(*conditions)

            row = an.fetch_one(statement)
            if not row:
                return None

            llm_events = int(row["llm_events"] or 0)
            llm_cost = an.cost_usd(row["cost_nano"])

            return {
                "total_events": row["total_events"],
                "user_email": row["user_email"],
                "kpis": {
                    "total_events": row["total_events"],
                    "active_days": row["active_days"],
                    "llm_events": llm_events,
                    "tool_events": int(row["tool_events"] or 0),
                    "agent_events": int(row["agent_events"] or 0),
                    # No socketio-style chat event exists in usage_event; the UI renders this
                    # field (KPI card and daily chart), so the key stays, always zero
                    "chat_events": 0,
                    "errors": int(row["errors"] or 0),
                    "input_tokens": int(row["input_tokens"] or 0),
                    "output_tokens": int(row["output_tokens"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                    "cache_read_tokens": int(row["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(row["cache_creation_tokens"] or 0),
                    "llm_cost": llm_cost,
                    **an.cost_split_usd(row),
                    "avg_cost_per_call": (llm_cost / llm_events) if llm_cost and llm_events else 0.0,
                },
            }

        @staticmethod
        def _models(project_id, conditions):
            """ Models used by this user """
            rows = an.fetch_all(select(
                UsageEvent.model_name,
                func.count().label("calls"),
            ).where(
                *conditions,
                UsageEvent.model_name.isnot(None),
                UsageEvent.model_name != "",
            ).group_by(
                UsageEvent.model_name,
            ).order_by(func.count().desc()))

            display_names = an.model_display_names(project_id) if rows else {}

            return [
                {
                    "model_name": r["model_name"],
                    "display_name": display_names.get(r["model_name"], r["model_name"]),
                    "calls": r["calls"],
                }
                for r in rows
            ]

        @staticmethod
        def _tools(conditions):
            """ Tools used by this user """
            rows = an.fetch_all(select(
                UsageEvent.tool_name,
                func.count().label("calls"),
            ).where(
                *conditions,
                UsageEvent.tool_name.isnot(None),
                UsageEvent.tool_name != "",
            ).group_by(
                UsageEvent.tool_name,
            ).order_by(func.count().desc()))

            return [
                {"tool_name": r["tool_name"], "calls": r["calls"]}
                for r in rows
            ]

        @staticmethod
        def _agents(conditions):
            """Agents (applications/pipelines) this user ran.

            usage_event tags every llm/tool row with its run's own root_entity_type/id, but
            has no root_entity_name column — only entity_name, which names the node that made
            one call. The name is therefore read off whichever row IS the run
            (entity_id == root_entity_id) so a nested run cannot title its parent with a
            sub-agent's name.
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
                func.count(distinct(UsageEvent.run_id)).label("runs"),
            ).where(
                *conditions,
                an.is_agent_row(),
                UsageEvent.root_entity_id.isnot(None),
            ).group_by(
                UsageEvent.root_entity_type,
                UsageEvent.root_entity_id,
            ).order_by(func.count(distinct(UsageEvent.run_id)).desc()).limit(_AGENT_LIMIT))

            return [
                {
                    "entity_name": r["entity_name"] or f"Agent #{r['root_entity_id']}",
                    "entity_id": r["root_entity_id"],
                    "runs": r["runs"],
                }
                for r in rows
            ]

        @staticmethod
        def _daily_activity(conditions):
            """ Daily activity by event type """
            day = an.day_expr().label("day")

            rows = an.fetch_all(select(
                day,
                an.llm_calls_expr().label("llm"),
                an.tool_runs_expr().label("tool"),
                an.agent_runs_expr().label("agent"),
                func.count().label("total"),
            ).where(*conditions).group_by(day).order_by(day))

            return [
                {
                    "date": r["day"].isoformat() if r["day"] else None,
                    "llm": int(r["llm"] or 0),
                    "tool": int(r["tool"] or 0),
                    # No socketio-style chat event exists in usage_event; the daily chart
                    # renders this series, so the key stays, always zero
                    "chat": 0,
                    "agent": int(r["agent"] or 0),
                    "total": r["total"] or 0,
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

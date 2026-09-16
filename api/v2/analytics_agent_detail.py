"""
Single-agent analytics detail endpoint (usage_event port of elitea_core's
analytics_agent_detail.py).

The external query parameter is still called entity_id (the UI sends that name), but it is
matched against root_entity_id: an agent's identity is the run, not any one call inside it.
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

    _USERS_LIMIT = 50
    _TOOLS_LIMIT = 30

    class PromptLibAPI(api_tools.APIModeHandler):
        """Full breakdown for one agent/pipeline."""

        @register_openapi(
            name="Get Agent Analytics Detail",
            description=(
                "Returns the full usage breakdown for one agent/pipeline: KPIs, its users, "
                "the tools it called, and its daily activity trend."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need the full drill-down for one specific agent or pipeline: its KPIs, which users ran it, which tools it called, and its daily trend. Do not use this tool for a leaderboard across agents — use List Agent Analytics. Do not use for project-wide KPIs — use Get Project AI Analytics.",
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
                    "name": "entity_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "integer"},
                    "description": "Agent/pipeline root entity ID (the run's root_entity_id).",
                    "example": 7,
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
                    "description": "Agent analytics detail",
                    "content": {
                        "application/json": {
                            "example": {
                                "entity_name": "Code Review Bot",
                                "entity_id": 7,
                                "kpis": {
                                    "total_events": 95,
                                    "unique_users": 5,
                                    "avg_duration_ms": 1200.0,
                                    "errors": 4,
                                    "error_rate": 4.2,
                                    "input_tokens": 40000,
                                    "output_tokens": 44500,
                                    "total_tokens": 84500,
                                    "cache_read_tokens": 0,
                                    "cache_creation_tokens": 0,
                                    "llm_cost": 0.00845,
                                    "input_cost": 0.004,
                                    "output_cost": 0.00445,
                                    "cache_read_cost": 0.0,
                                    "cache_creation_cost": 0.0,
                                    "avg_cost_per_call": 0.0001,
                                },
                                "users_total": 5,
                                "users_truncated": False,
                                "users": [
                                    {
                                        "user_id": 11,
                                        "user_email": "user@example.com",
                                        "events": 40,
                                        "avg_duration_ms": 1100.0,
                                        "errors": 2,
                                    }
                                ],
                                "tools": [
                                    {"tool_name": "web_search", "calls": 30},
                                ],
                                "daily_usage": [
                                    {"date": "2025-01-01", "events": 20, "errors": 1},
                                ],
                            },
                        }
                    },
                },
                "401": {"description": "Unauthorized"},
                "404": {"description": "Agent not found in range"},
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
            GET /api/v2/usage/analytics_agent_detail/prompt_lib/<project_id>
            """
            try:
                entity_id = an.clamp_int(request.args.get("entity_id"), None)
                if entity_id is None:
                    return {"error": "entity_id is required"}, 400

                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = an.base_filters(project_id, dt_from, dt_to) + [
                    an.is_agent_row(),
                    UsageEvent.root_entity_id == entity_id,
                ]

                entity_name, kpis = self._kpis(conditions, entity_id)
                if kpis["total_events"] == 0:
                    return {"error": "Agent not found in the selected range"}, 404

                users_total, users_truncated, users = self._users(conditions)

                return {
                    "entity_name": entity_name,
                    "entity_id": entity_id,
                    "kpis": kpis,
                    "users_total": users_total,
                    "users_truncated": users_truncated,
                    "users": users,
                    "tools": self._tools(conditions),
                    "daily_usage": self._daily_usage(conditions),
                }, 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics agent detail query failed", exc_info=True)
                return {"error": "Failed to query analytics agent detail"}, 500

        @staticmethod
        def _kpis(conditions, entity_id):
            """Display name plus the headline numbers, in one scan."""
            try:
                from plugins.costs.models.model_price import ModelPrice
                _model_price_available = True
            except ImportError:
                ModelPrice = None
                _model_price_available = False

            # No root_entity_name column: read the name off whichever row IS the run
            # (entity_id == root_entity_id).
            entity_name_expr = func.max(case(
                (UsageEvent.entity_id == UsageEvent.root_entity_id, UsageEvent.entity_name),
                else_=None,
            ))

            columns = [
                entity_name_expr.label("entity_name"),
                an.agent_runs_expr().label("total_events"),
                func.count(distinct(UsageEvent.user_id)).label("unique_users"),
                func.avg(UsageEvent.duration_ms).label("avg_duration_ms"),
                # Failed runs, so error_rate stays a percentage of total_events (also runs)
                an.agent_error_runs_expr().label("errors"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(
                    func.coalesce(UsageEvent.cache_creation_tokens, 0)
                ).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                # No error-row exclusion (D1): a provider that charged for a failed call still
                # spent the tokens.
                func.sum(func.coalesce(UsageEvent.cost_micro_usd, 0)).label("cost_micro"),
                an.llm_calls_expr().label("llm_calls"),
            ]

            if _model_price_available:
                # model_name is unique on model_prices, so this outerjoin cannot fan out.
                columns += [
                    func.sum(
                        an.billable_input_expr()
                        * func.coalesce(ModelPrice.input_cost_per_token, 0)
                    ).label("input_cost"),
                    func.sum(
                        func.coalesce(UsageEvent.output_tokens, 0)
                        * func.coalesce(ModelPrice.output_cost_per_token, 0)
                    ).label("output_cost"),
                    func.sum(
                        func.coalesce(UsageEvent.cache_read_tokens, 0)
                        * func.coalesce(ModelPrice.cache_read_input_token_cost, 0)
                    ).label("cache_read_cost"),
                    func.sum(
                        func.coalesce(UsageEvent.cache_creation_tokens, 0)
                        * func.coalesce(ModelPrice.cache_creation_input_token_cost, 0)
                    ).label("cache_creation_cost"),
                ]

            stmt = select(*columns).where(*conditions)
            if _model_price_available:
                stmt = stmt.select_from(UsageEvent).outerjoin(
                    ModelPrice, UsageEvent.model_name == ModelPrice.model_name,
                )

            row = an.fetch_one(stmt)
            #
            total_events = int(row["total_events"] or 0) if row else 0
            errors = int(row["errors"] or 0) if row else 0
            llm_calls = int(row["llm_calls"] or 0) if row else 0
            llm_cost = an.cost_usd(row["cost_micro"] if row else 0)
            #
            entity_name = (row["entity_name"] if row else None) or f"Agent #{entity_id}"
            #
            kpis = {
                "total_events": total_events,
                "unique_users": (row["unique_users"] or 0) if row else 0,
                "avg_duration_ms": an.rounded(row["avg_duration_ms"], 1) if row else 0,
                "errors": errors,
                "error_rate": round(errors / total_events * 100, 2) if total_events > 0 else 0,
                "input_tokens": int(row["input_tokens"] or 0) if row else 0,
                "output_tokens": int(row["output_tokens"] or 0) if row else 0,
                "total_tokens": int(row["total_tokens"] or 0) if row else 0,
                "cache_read_tokens": int(row["cache_read_tokens"] or 0) if row else 0,
                "cache_creation_tokens": int(row["cache_creation_tokens"] or 0) if row else 0,
                "llm_cost": llm_cost,
                "input_cost": round(float(row["input_cost"]), 6) if _model_price_available and row and row["input_cost"] else 0.0,
                "output_cost": round(float(row["output_cost"]), 6) if _model_price_available and row and row["output_cost"] else 0.0,
                "cache_read_cost": round(float(row["cache_read_cost"]), 6) if _model_price_available and row and row["cache_read_cost"] else 0.0,
                "cache_creation_cost": round(float(row["cache_creation_cost"]), 6) if _model_price_available and row and row["cache_creation_cost"] else 0.0,
                "avg_cost_per_call": round(llm_cost / llm_calls, 6) if llm_calls else 0,
            }
            #
            return entity_name, kpis

        @staticmethod
        def _users(conditions):
            """Per-user breakdown, grouped by user_id alone.

            user_email is null on rows written before the write path populated it, and
            grouping by the (user_id, user_email) pair would split one person into several
            rows whenever both a null and a populated email exist for them.
            """
            events_col = an.agent_runs_expr().label("events")
            #
            rows = an.fetch_all(select(
                UsageEvent.user_id,
                func.max(UsageEvent.user_email).label("user_email"),
                events_col,
                func.avg(UsageEvent.duration_ms).label("avg_duration_ms"),
                an.agent_error_runs_expr().label("errors"),
            ).where(*conditions).group_by(
                UsageEvent.user_id,
            ).order_by(events_col.desc()).limit(_USERS_LIMIT))
            #
            total_row = an.fetch_one(select(
                func.count(distinct(UsageEvent.user_id)).label("total"),
            ).where(*conditions))
            users_total = (total_row["total"] if total_row else 0) or 0
            #
            emails = an.label_users(
                [r["user_id"] for r in rows],
                {r["user_id"]: r["user_email"] for r in rows if r["user_id"] is not None},
            )
            #
            users = [
                {
                    "user_id": r["user_id"],
                    "user_email": emails.get(r["user_id"]),
                    "events": int(r["events"] or 0),
                    "avg_duration_ms": an.rounded(r["avg_duration_ms"], 1),
                    "errors": int(r["errors"] or 0),
                }
                for r in rows
            ]
            #
            return users_total, users_total > len(users), users

        @staticmethod
        def _tools(conditions):
            """Tool calls under this agent's runs.

            root_entity_id sits directly on every tool row, so no trace_id correlation is
            needed to attribute a call back to the agent.
            """
            calls_col = func.count().label("calls")
            #
            rows = an.fetch_all(select(
                UsageEvent.tool_name,
                calls_col,
            ).where(
                *conditions,
                UsageEvent.event_type == an.EVENT_TOOL,
                UsageEvent.tool_name.isnot(None),
                UsageEvent.tool_name != "",
            ).group_by(
                UsageEvent.tool_name,
            ).order_by(calls_col.desc()).limit(_TOOLS_LIMIT))
            #
            return [
                {"tool_name": r["tool_name"], "calls": r["calls"]}
                for r in rows
            ]

        @staticmethod
        def _daily_usage(conditions):
            """Daily run count and errors."""
            day = an.day_expr().label("day")
            events_col = an.agent_runs_expr().label("events")
            #
            rows = an.fetch_all(select(
                day,
                events_col,
                an.agent_error_runs_expr().label("errors"),
            ).where(*conditions).group_by(day).order_by(day))
            #
            return [
                {
                    "date": r["day"].isoformat() if r["day"] else None,
                    "events": int(r["events"] or 0),
                    "errors": int(r["errors"] or 0),
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

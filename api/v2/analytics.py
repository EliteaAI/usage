"""
Project AI-adoption analytics over usage_event.

Serves the Overview tab's AI half: adoption KPIs, the AI adopter leaderboard, the daily
AI-activity trend and the per-model breakdown. The tracing half of the Overview payload
(event-type breakdown, chat counts, health) stays in elitea_core on audit_events, because
socketio chat activity has no equivalent in usage_event.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import distinct, func, select

    from ...methods import _analytics as an
    from ...models.usage_event import UsageEvent

    _MODEL_LIMIT = 20
    _LEADERBOARD_LIMIT = 5

    class PromptLibAPI(api_tools.APIModeHandler):
        """AI adoption analytics for one project."""

        @register_openapi(
            name="Get Project AI Analytics",
            description=(
                "Returns AI adoption KPIs, the top AI adopters, the daily AI activity trend "
                "and the per-model breakdown for a project, aggregated from metered LLM and "
                "tool calls in usage_event."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need a project's AI adoption dashboard: how many members use AI, how many LLM calls, tool runs and agent runs happened, how many tokens and how much cost they consumed, who the top AI adopters are, and the daily trend. Do not use this tool for per-user, per-tool or per-agent drill-downs — use the corresponding list endpoints. Do not use it for chat message counts or error-health views, which come from the tracing analytics endpoint.",
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
            ],
            responses={
                "200": {
                    "description": "AI adoption analytics for the project",
                    "content": {
                        "application/json": {
                            "example": {
                                "kpis": {
                                    "unique_users": 4,
                                    "total_project_users": 6,
                                    "ai_active_users": 4,
                                    "adoption_rate": 66.7,
                                    "llm_calls": 120,
                                    "tool_runs": 45,
                                    "agent_runs": 12,
                                    "total_tokens": 98304,
                                    "total_llm_cost": 1.234567,
                                    "unique_tools": 7,
                                    "unique_models": 3,
                                },
                                "top_ai_users": [
                                    {
                                        "user_id": 11,
                                        "user_email": "user@example.com",
                                        "ai_events": 80,
                                        "llm_calls": 60,
                                        "tool_runs": 20,
                                        "agent_runs": 5,
                                    }
                                ],
                                "daily_activity": [
                                    {
                                        "date": "2025-01-01",
                                        "llm_calls": 20,
                                        "tool_runs": 8,
                                        "agent_runs": 2,
                                        "active_users": 3,
                                    }
                                ],
                                "models": [
                                    {
                                        "model_name": "gpt-4o",
                                        "display_name": "GPT-4o",
                                        "calls": 60,
                                        "users": 3,
                                        "avg_duration_ms": 812.4,
                                    }
                                ],
                            },
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
            GET /api/v2/usage/analytics/prompt_lib/<project_id>
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = an.base_filters(project_id, dt_from, dt_to)
                #
                kpis = self._kpis(project_id, conditions)
                #
                return {
                    "kpis": kpis,
                    "top_ai_users": self._top_ai_users(conditions),
                    "daily_activity": self._daily_activity(conditions),
                    "models": self._models(project_id, conditions),
                }, 200
            except Exception:  # pylint: disable=W0703
                log.error("Usage analytics query failed", exc_info=True)
                return {"error": "Failed to query analytics"}, 500

        @staticmethod
        def _kpis(project_id, conditions):
            """One scan for every headline number."""
            row = an.fetch_one(select(
                func.count(distinct(UsageEvent.user_id)).label("unique_users"),
                an.llm_calls_expr().label("llm_calls"),
                an.tool_runs_expr().label("tool_runs"),
                an.agent_runs_expr().label("agent_runs"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
                # nullif so a blank name is not its own distinct value
                func.count(distinct(func.nullif(UsageEvent.tool_name, ""))).label("unique_tools"),
                func.count(distinct(func.nullif(UsageEvent.model_name, ""))).label("unique_models"),
            ).where(*conditions))
            #
            unique_users = (row["unique_users"] if row else 0) or 0
            total_project_users = an.project_member_count(project_id, unique_users)
            #
            # Every usage_event row is a metered LLM or tool call, so anyone who appears here did
            # AI work: ai_active_users is unique_users by construction. The old audit_events
            # figures diverged only because that table also logged page views and API requests —
            # including the analytics requests themselves, which is defect #5920.
            ai_active_users = unique_users
            #
            # Against project membership, not against active users. The old denominator was
            # unique_users, which on this table is the same set and would peg adoption at 100%;
            # the card reads "% adoption", and the meaningful share is of the team.
            adoption_rate = round(
                ai_active_users / total_project_users * 100, 1,
            ) if total_project_users > 0 else 0
            #
            return {
                "unique_users": unique_users,
                "total_project_users": total_project_users,
                "ai_active_users": ai_active_users,
                "adoption_rate": adoption_rate,
                "llm_calls": int(row["llm_calls"] or 0) if row else 0,
                "tool_runs": int(row["tool_runs"] or 0) if row else 0,
                "agent_runs": int(row["agent_runs"] or 0) if row else 0,
                "total_tokens": int(row["total_tokens"] or 0) if row else 0,
                "total_llm_cost": an.cost_usd(row["cost_nano"] if row else 0),
                "unique_tools": int(row["unique_tools"] or 0) if row else 0,
                "unique_models": int(row["unique_models"] or 0) if row else 0,
            }

        @staticmethod
        def _top_ai_users(conditions):
            """Top AI adopters.

            Grouped by user_id alone: user_email is null on rows written before the write path
            populated it, and grouping by the pair would split one person into several rows.
            """
            rows = an.fetch_all(select(
                UsageEvent.user_id,
                func.max(UsageEvent.user_email).label("user_email"),
                func.count().label("ai_events"),
                an.llm_calls_expr().label("llm_calls"),
                an.tool_runs_expr().label("tool_runs"),
                an.agent_runs_expr().label("agent_runs"),
            ).where(*conditions).group_by(
                UsageEvent.user_id,
            ).order_by(func.count().desc()).limit(_LEADERBOARD_LIMIT))
            #
            emails = an.label_users(
                [r["user_id"] for r in rows],
                {r["user_id"]: r["user_email"] for r in rows if r["user_id"] is not None},
            )
            #
            return [
                {
                    "user_id": r["user_id"],
                    "user_email": emails.get(r["user_id"]),
                    "ai_events": r["ai_events"],
                    "llm_calls": int(r["llm_calls"] or 0),
                    "tool_runs": int(r["tool_runs"] or 0),
                    "agent_runs": int(r["agent_runs"] or 0),
                }
                for r in rows
            ]

        @staticmethod
        def _daily_activity(conditions):
            """Daily AI activity. Error and total-event series stay on the tracing payload."""
            day = an.day_expr().label("day")
            #
            rows = an.fetch_all(select(
                day,
                an.llm_calls_expr().label("llm_calls"),
                an.tool_runs_expr().label("tool_runs"),
                an.agent_runs_expr().label("agent_runs"),
                func.count(distinct(UsageEvent.user_id)).label("active_users"),
            ).where(*conditions).group_by(day).order_by(day))
            #
            return [
                {
                    "date": r["day"].isoformat() if r["day"] else None,
                    "llm_calls": int(r["llm_calls"] or 0),
                    "tool_runs": int(r["tool_runs"] or 0),
                    "agent_runs": int(r["agent_runs"] or 0),
                    "active_users": r["active_users"] or 0,
                }
                for r in rows
            ]

        @staticmethod
        def _models(project_id, conditions):
            """ Per-model call volume """
            rows = an.fetch_all(select(
                UsageEvent.model_name,
                func.count().label("calls"),
                func.count(distinct(UsageEvent.user_id)).label("users"),
                func.avg(UsageEvent.duration_ms).label("avg_duration_ms"),
            ).where(
                *conditions,
                UsageEvent.model_name.isnot(None),
                UsageEvent.model_name != "",
            ).group_by(
                UsageEvent.model_name,
            ).order_by(func.count().desc()).limit(_MODEL_LIMIT))
            #
            display_names = an.model_display_names(project_id) if rows else {}
            #
            return [
                {
                    "model_name": r["model_name"],
                    "display_name": display_names.get(r["model_name"], r["model_name"]),
                    "calls": r["calls"],
                    "users": r["users"],
                    "avg_duration_ms": an.rounded(r["avg_duration_ms"], 1),
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

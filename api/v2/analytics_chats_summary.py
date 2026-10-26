"""
Chat analytics summary endpoint.

Returns KPIs, participant type breakdown, and optional daily series for all user chats
in a project within a date range.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    import datetime

    from flask import request
    from sqlalchemy import Date, cast, distinct, func, select

    from ...methods import _analytics as an
    from ...methods import analytics_chats as ac
    from ...models.usage_event import UsageEvent

    # Normalise root_entity_type values to human-readable participant type labels
    _PARTICIPANT_TYPE_MAP = {
        None: "model",
        "application": "agent",
        "pipeline": "pipeline",
        "skill": "skill",
    }

    class PromptLibAPI(api_tools.APIModeHandler):
        """Chat analytics summary: KPIs, participant breakdown, and optional daily series."""

        @register_openapi(
            name="Get Chat Analytics Summary",
            description=(
                "Returns project-level KPIs for user chat conversations, a breakdown by "
                "participant type (model, agent, pipeline, skill), and an optional daily series."
            ),
            mcp_tool=True,
            mcp_description=(
                "Use this tool for project-wide chat analytics KPIs: active chats, responses, "
                "users, errors, tokens and cost. Also provides a per-participant-type breakdown "
                "and optional daily trend."
            ),
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
                    "name": "include_daily",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "enum": ["true", "false"], "default": "false"},
                    "description": "Include daily activity series in the response.",
                },
            ],
            responses={
                "200": {
                    "description": "Chat analytics summary",
                    "content": {
                        "application/json": {
                            "example": {
                                "kpis": {
                                    "active_chats": 15,
                                    "responses": 120,
                                    "users": 8,
                                    "errors": 3,
                                    "llm_cost": 0.012,
                                    "input_cost": 0.005,
                                    "output_cost": 0.007,
                                    "cache_read_cost": 0.0,
                                    "cache_creation_cost": 0.0,
                                    "total_tokens": 50000,
                                    "input_tokens": 20000,
                                    "output_tokens": 30000,
                                    "cache_read_tokens": 0,
                                    "cache_creation_tokens": 0,
                                    "avg_cost_per_chat": 0.0008,
                                    "avg_cost_per_response": 0.0001,
                                    "project_cost": 0.1,
                                    "share_of_project_cost": 0.12,
                                    "below_resolution": {},
                                },
                                "by_participant_type": [
                                    {"type": "model", "responses": 90, "total_tokens": 35000, "llm_cost": 0.0088},
                                    {"type": "agent", "responses": 30, "total_tokens": 15000, "llm_cost": 0.0032},
                                ],
                                "daily": [],
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
            GET /api/v2/usage/analytics_chats_summary/prompt_lib/<project_id>
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                include_daily = request.args.get("include_daily", "false").lower() == "true"

                conditions = ac.chat_base_filters(project_id, dt_from, dt_to)

                # KPI query
                active_chats_col = func.count(distinct(UsageEvent.conversation_id)).label("active_chats")
                responses_col = func.count(distinct(UsageEvent.run_id)).label("responses")
                users_col = func.count(distinct(UsageEvent.user_id)).label("users")
                errors_col = ac.chat_error_responses_expr().label("errors")

                input_tokens_col = func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens")
                output_tokens_col = func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens")
                cache_read_tokens_col = func.sum(
                    func.coalesce(UsageEvent.cache_read_tokens, 0)
                ).label("cache_read_tokens")
                cache_creation_tokens_col = func.sum(
                    func.coalesce(UsageEvent.cache_creation_tokens, 0)
                ).label("cache_creation_tokens")
                total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
                cost_nano_col = func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano")

                cost_split_cols = an.cost_split_sums()
                below_res_cols = an.below_resolution_sums()

                kpi_stmt = select(
                    active_chats_col,
                    responses_col,
                    users_col,
                    errors_col,
                    input_tokens_col,
                    output_tokens_col,
                    cache_read_tokens_col,
                    cache_creation_tokens_col,
                    total_tokens_col,
                    cost_nano_col,
                    *cost_split_cols,
                    *below_res_cols,
                ).where(*conditions)

                row = an.fetch_one(kpi_stmt) or {}

                active_chats = int(row.get("active_chats") or 0)
                responses = int(row.get("responses") or 0)
                llm_cost = an.cost_usd(row.get("cost_nano") or 0)

                # Project total cost (no conversation filter) for share_of_project_cost
                project_conditions = an.base_filters(project_id, dt_from, dt_to)
                project_cost_row = an.fetch_one(
                    select(func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("project_cost_nano"))
                    .where(*project_conditions)
                ) or {}
                project_cost = an.cost_usd(project_cost_row.get("project_cost_nano") or 0)
                share = round(llm_cost / project_cost, 6) if project_cost else 0.0

                kpis = {
                    "active_chats": active_chats,
                    "responses": responses,
                    "users": int(row.get("users") or 0),
                    "errors": int(row.get("errors") or 0),
                    "llm_cost": llm_cost,
                    **an.cost_split_usd(row),
                    "total_tokens": int(row.get("total_tokens") or 0),
                    "input_tokens": int(row.get("input_tokens") or 0),
                    "output_tokens": int(row.get("output_tokens") or 0),
                    "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
                    "cache_creation_tokens": int(row.get("cache_creation_tokens") or 0),
                    "avg_cost_per_chat": round(llm_cost / active_chats, 9) if active_chats else 0,
                    "avg_cost_per_response": round(llm_cost / responses, 9) if responses else 0,
                    "project_cost": project_cost,
                    "share_of_project_cost": share,
                    "below_resolution": an.below_resolution(row),
                }

                # Participant type breakdown
                by_participant_type = self._participant_breakdown(conditions)

                # Optional daily series
                daily = []
                if include_daily:
                    daily = self._daily_series(conditions, dt_from, dt_to)

                return {
                    "kpis": kpis,
                    "by_participant_type": by_participant_type,
                    "daily": daily,
                }, 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics chats summary query failed", exc_info=True)
                return {"error": "Failed to query analytics chats summary"}, 500

        @staticmethod
        def _participant_breakdown(conditions):
            """Responses, tokens and cost grouped by root_entity_type."""
            type_col = func.coalesce(UsageEvent.root_entity_type, "model").label("participant_type_raw")
            responses_col = func.count(distinct(UsageEvent.run_id)).label("responses")
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            cost_nano_col = func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano")

            rows = an.fetch_all(
                select(type_col, responses_col, total_tokens_col, cost_nano_col)
                .where(*conditions)
                .group_by(func.coalesce(UsageEvent.root_entity_type, "model"))
                .order_by(func.count(distinct(UsageEvent.run_id)).desc())
            )

            return [
                {
                    "type": _PARTICIPANT_TYPE_MAP.get(r["participant_type_raw"], r["participant_type_raw"]),
                    "responses": int(r["responses"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(r["cost_nano"]),
                }
                for r in rows
            ]

        @staticmethod
        def _daily_series(conditions, dt_from, dt_to):
            """Daily aggregation padded with zeros for every date in [dt_from, dt_to]."""
            date_col = cast(UsageEvent.ts, Date).label("date")
            responses_col = func.count(distinct(UsageEvent.run_id)).label("responses")
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            cost_nano_col = func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano")

            rows = an.fetch_all(
                select(date_col, responses_col, total_tokens_col, cost_nano_col)
                .where(*conditions)
                .group_by(date_col)
                .order_by(date_col)
            )

            data_by_date = {
                r["date"].isoformat(): {
                    "responses": int(r["responses"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(r["cost_nano"]),
                }
                for r in rows
                if r["date"]
            }

            # Build a zero-padded series spanning the full requested date range
            start = dt_from.date() if hasattr(dt_from, "date") else dt_from
            end = dt_to.date() if hasattr(dt_to, "date") else dt_to
            result = []
            current = start
            while current <= end:
                iso = current.isoformat()
                entry = data_by_date.get(iso, {"responses": 0, "total_tokens": 0, "llm_cost": 0.0})
                result.append({"date": iso, **entry})
                current += datetime.timedelta(days=1)
            return result


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

"""
Analytics cost breakdown, ported onto usage_event (#6574).

Feeds both the Costs and Tokens analytics tabs from one payload: KPI totals, per-model,
per-agent, per-user cost/token breakdowns, and a daily trend. Every cost field — the total and
its four components alike — is summed straight off the columns usage_event recorded at meter
time. Nothing here consults the price catalog, so editing a model's price changes what the next
call costs and leaves what a past call cost exactly as it was charged.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import case, func, select

    from ...methods import _analytics as an
    from ...models.usage_event import UsageEvent

    _MODEL_LIMIT = 30
    _AGENT_LIMIT = 20
    _USER_LIMIT = 20

    class PromptLibAPI(api_tools.APIModeHandler):
        """LLM cost breakdown analytics for the project."""

        @register_openapi(
            name="Get Analytics Cost Breakdown",
            description=(
                "Returns cost and token KPIs plus per-model, per-agent, per-user and daily "
                "breakdowns of LLM spend for a project, aggregated from usage_event."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need LLM cost or token breakdowns by model, by agent, by user, or over time. Do not use this tool for AI adoption KPIs or a project's overall activity dashboard — use Get Project AI Analytics. Do not use for per-tool drill-downs — use the tool analytics endpoints. This endpoint is best for 'where is the money/tokens going.'",
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
                    "description": "LLM cost breakdown",
                    "content": {
                        "application/json": {
                            "example": {
                                "kpis": {
                                    "total_cost": 12.45,
                                    "total_input_tokens": 4500000,
                                    "total_output_tokens": 980000,
                                    "total_cache_read_tokens": 120000,
                                    "total_cache_creation_tokens": 30000,
                                    "total_tokens": 5480000,
                                    "avg_cost_per_call": 0.0016,
                                    "total_input_cost": 8.0,
                                    "total_output_cost": 3.5,
                                    "total_cache_read_cost": 0.6,
                                    "total_cache_creation_cost": 0.35,
                                },
                                "by_model": [
                                    {
                                        "model_name": "gpt-4o",
                                        "display_name": "GPT-4o",
                                        "calls": 450,
                                        "input_tokens": 3200000,
                                        "output_tokens": 720000,
                                        "cache_read_tokens": 90000,
                                        "cache_creation_tokens": 20000,
                                        "total_tokens": 4030000,
                                        "total_cost": 9.80,
                                        "input_cost": 6.4,
                                        "output_cost": 2.6,
                                        "cache_read_cost": 0.5,
                                        "cache_creation_cost": 0.3,
                                    }
                                ],
                                "by_agent": [
                                    {
                                        "entity_name": "Code Review Bot",
                                        "entity_id": 7,
                                        "total_cost": 4.20,
                                        "input_cost": 2.9,
                                        "output_cost": 1.1,
                                        "cache_read_cost": 0.1,
                                        "cache_creation_cost": 0.1,
                                        "input_tokens": 1500000,
                                        "output_tokens": 500000,
                                        "cache_read_tokens": 70000,
                                        "cache_creation_tokens": 30000,
                                        "total_tokens": 2100000,
                                        "calls": 300,
                                        "avg_cost": 0.014,
                                    }
                                ],
                                "by_user": [
                                    {
                                        "user_id": 42,
                                        "user_email": "alice@example.com",
                                        "total_cost": 3.10,
                                        "input_cost": 2.0,
                                        "output_cost": 1.0,
                                        "cache_read_cost": 0.05,
                                        "cache_creation_cost": 0.05,
                                        "input_tokens": 1100000,
                                        "output_tokens": 400000,
                                        "cache_read_tokens": 30000,
                                        "cache_creation_tokens": 20000,
                                        "total_tokens": 1550000,
                                    }
                                ],
                                "daily": [
                                    {
                                        "date": "2025-01-15",
                                        "total_cost": 1.80,
                                        "input_cost": 1.2,
                                        "output_cost": 0.5,
                                        "cache_read_cost": 0.05,
                                        "cache_creation_cost": 0.05,
                                        "input_tokens": 650000,
                                        "output_tokens": 220000,
                                        "cache_read_tokens": 20000,
                                        "cache_creation_tokens": 10000,
                                        "total_tokens": 900000,
                                    }
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
            GET /api/v2/usage/analytics_costs/prompt_lib/<project_id>
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                # No is_error filter: a provider that reported tokens alongside a 4xx has still
                # charged for them, so error rows stay in every sum below (#6574).
                conditions = an.base_filters(project_id, dt_from, dt_to) + [
                    UsageEvent.event_type == an.EVENT_LLM,
                ]

                return {
                    "kpis": self._kpis(conditions),
                    "by_model": self._by_model(project_id, conditions),
                    "by_agent": self._by_agent(conditions),
                    "by_user": self._by_user(conditions),
                    "daily": self._daily(conditions),
                }, 200

            except Exception:
                log.error("Analytics cost query failed", exc_info=True)
                return {"error": "Failed to query analytics costs"}, 500

        @staticmethod
        def _kpis(conditions):
            """One scan for every headline number."""
            columns = [
                func.sum(func.coalesce(UsageEvent.cost_micro_usd, 0)).label("cost_micro"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("total_input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("total_output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("total_cache_read_tokens"),
                func.sum(
                    func.coalesce(UsageEvent.cache_creation_tokens, 0)
                ).label("total_cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.count().label("total_calls"),
            ] + an.cost_split_sums()

            # An ungrouped aggregate always returns exactly one row; the fallback is belt-and-braces.
            row = an.fetch_one(select(*columns).where(*conditions)) or {}

            total_cost = an.cost_usd(row.get("cost_micro"))
            total_calls = row.get("total_calls") or 0

            return {
                "total_cost": total_cost,
                "total_input_tokens": int(row.get("total_input_tokens") or 0),
                "total_output_tokens": int(row.get("total_output_tokens") or 0),
                "total_cache_read_tokens": int(row.get("total_cache_read_tokens") or 0),
                "total_cache_creation_tokens": int(row.get("total_cache_creation_tokens") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
                "avg_cost_per_call": round(total_cost / total_calls, 8) if total_calls > 0 else 0.0,
                **{
                    f"total_{key}": value
                    for key, value in an.cost_split_usd(row).items()
                },
            }

        @staticmethod
        def _by_model(project_id, conditions):
            """Cost/token breakdown per model."""
            columns = [
                UsageEvent.model_name,
                func.count().label("calls"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_creation_tokens, 0)).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_micro_usd, 0)).label("cost_micro"),
            ] + an.cost_split_sums()

            statement = select(*columns).where(
                *conditions, UsageEvent.model_name.isnot(None), UsageEvent.model_name != "",
            ).group_by(UsageEvent.model_name).order_by(
                func.sum(UsageEvent.cost_micro_usd).desc(),
            ).limit(_MODEL_LIMIT)

            rows = an.fetch_all(statement)
            display_names = an.model_display_names(project_id) if rows else {}

            return [
                {
                    "model_name": r["model_name"],
                    "display_name": display_names.get(r["model_name"], r["model_name"]),
                    "calls": r["calls"],
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "total_cost": an.cost_usd(r["cost_micro"]),
                    **an.cost_split_usd(r),
                }
                for r in rows
            ]

        @staticmethod
        def _by_agent(conditions):
            """Cost/token breakdown per agent run.

            Grouped by (root_entity_type, root_entity_id): every llm row already carries the
            root of the run it belongs to, so this reads straight off the row instead of the
            elitea_core original's trace_id join to a separate application-type event.

            entity_name names the node that made one call and there is no root_entity_name, so
            the label is read only off whichever row IS the run (entity_id == root_entity_id) —
            otherwise a nested run titles its parent with a sub-agent's name.
            """
            root_name = func.max(case(
                (UsageEvent.entity_id == UsageEvent.root_entity_id, UsageEvent.entity_name),
                else_=None,
            ))
            columns = [
                UsageEvent.root_entity_id,
                root_name.label("entity_name"),
                func.count().label("calls"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_creation_tokens, 0)).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_micro_usd, 0)).label("cost_micro"),
            ] + an.cost_split_sums()

            statement = select(*columns).where(
                *conditions, an.is_agent_row(), UsageEvent.root_entity_id.isnot(None),
            ).group_by(
                UsageEvent.root_entity_type, UsageEvent.root_entity_id,
            ).order_by(func.sum(UsageEvent.cost_micro_usd).desc()).limit(_AGENT_LIMIT)

            rows = an.fetch_all(statement)

            return [
                {
                    "entity_name": r["entity_name"] or f"Agent #{r['root_entity_id']}",
                    "entity_id": r["root_entity_id"],
                    "total_cost": an.cost_usd(r["cost_micro"]),
                    **an.cost_split_usd(r),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "calls": r["calls"] or 0,
                    "avg_cost": (
                        round(an.cost_usd(r["cost_micro"]) / r["calls"], 6)
                        if r["cost_micro"] and r["calls"] else 0.0
                    ),
                }
                for r in rows
            ]

        @staticmethod
        def _by_user(conditions):
            """Cost/token breakdown per user.

            Grouped by user_id alone: user_email is null on rows written before the write path
            populated it, so grouping by the pair would split one person into several rows.
            """
            columns = [
                UsageEvent.user_id,
                func.max(UsageEvent.user_email).label("user_email"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_creation_tokens, 0)).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_micro_usd, 0)).label("cost_micro"),
            ] + an.cost_split_sums()

            statement = select(*columns).where(*conditions).group_by(
                UsageEvent.user_id,
            ).order_by(func.sum(UsageEvent.cost_micro_usd).desc()).limit(_USER_LIMIT)

            rows = an.fetch_all(statement)

            emails = an.label_users(
                [r["user_id"] for r in rows],
                {r["user_id"]: r["user_email"] for r in rows if r["user_id"] is not None},
            )

            return [
                {
                    "user_id": r["user_id"],
                    "user_email": emails.get(r["user_id"]),
                    "total_cost": an.cost_usd(r["cost_micro"]),
                    **an.cost_split_usd(r),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                }
                for r in rows
            ]

        @staticmethod
        def _daily(conditions):
            """ Daily cost/token trend """
            day = an.day_expr().label("day")

            columns = [
                day,
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_creation_tokens, 0)).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_micro_usd, 0)).label("cost_micro"),
            ] + an.cost_split_sums()

            statement = select(*columns).where(*conditions).group_by(day).order_by(day)

            rows = an.fetch_all(statement)

            return [
                {
                    "date": r["day"].isoformat() if r["day"] else None,
                    "total_cost": an.cost_usd(r["cost_micro"]),
                    **an.cost_split_usd(r),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
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

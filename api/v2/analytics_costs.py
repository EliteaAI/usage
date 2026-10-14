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
    from sqlalchemy import and_, case, distinct, func, select

    from ...methods import _analytics as an
    from ...methods import _skill_analytics as sa
    from ...models.usage_event import UsageEvent

    _MODEL_DISPLAY = 30
    _AGENT_DISPLAY = 20
    _SKILL_DISPLAY = 20
    _USER_DISPLAY = 20

    class PromptLibAPI(api_tools.APIModeHandler):
        """LLM cost breakdown analytics for the project."""

        @register_openapi(
            name="Get Analytics Cost Breakdown",
            description=(
                "Returns cost and token KPIs plus per-model, per-agent, per-skill, per-evaluation, "
                "per-user and daily breakdowns of LLM spend for a project, aggregated from usage_event. "
                "Evaluation spend is included in the totals, per-model, per-user and daily "
                "figures, and excluded from per-agent."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need LLM cost or token breakdowns by model, by agent, by skill, by user, or over time. Do not use this tool for AI adoption KPIs or a project's overall activity dashboard — use Get Project AI Analytics. Do not use for per-tool drill-downs — use the tool analytics endpoints. This endpoint is best for 'where is the money/tokens going.'",
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
                *an.RUN_SCOPE_PARAMETERS,
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
                                    "total_evaluation_cost": 0.42,
                                    "evaluation_calls": 60,
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
                                        "entity_kind": "agent",
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
                                "by_skill": [
                                    {
                                        "skill_id": 174,
                                        "skill_project_id": 1,
                                        "skill_name": "summarizer",
                                        "deleted": False,
                                        "total_cost": 0.84,
                                        "input_cost": 0.6,
                                        "output_cost": 0.2,
                                        "cache_read_cost": 0.03,
                                        "cache_creation_cost": 0.01,
                                        "input_tokens": 300000,
                                        "output_tokens": 90000,
                                        "cache_read_tokens": 12000,
                                        "cache_creation_tokens": 4000,
                                        "total_tokens": 406000,
                                        "calls": 40,
                                    }
                                ],
                                "by_evaluation": [
                                    {
                                        "entity_name": "Code Review Bot",
                                        "entity_id": 7,
                                        "entity_kind": "agent",
                                        "version_id": 12,
                                        "version_name": "base",
                                        "eval_runs": 3,
                                        "total_cost": 0.42,
                                        "input_cost": 0.3,
                                        "output_cost": 0.12,
                                        "cache_read_cost": 0.0,
                                        "cache_creation_cost": 0.0,
                                        "input_tokens": 150000,
                                        "output_tokens": 20000,
                                        "cache_read_tokens": 0,
                                        "cache_creation_tokens": 0,
                                        "total_tokens": 170000,
                                        "calls": 60,
                                        "avg_cost": 0.007,
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
                run_scope, error = an.request_run_scope(project_id, request.args)
                if error:
                    return error
                dt_from, dt_to = an.parse_date_range(request.args, run_scope=run_scope)
                # No is_error filter: a provider that reported tokens alongside a 4xx has still
                # charged for them, so error rows stay in every sum below (#6574).
                conditions = an.base_filters(project_id, dt_from, dt_to, run_scope=run_scope) + [
                    UsageEvent.event_type == an.EVENT_LLM,
                ]

                return {
                    "kpis": self._kpis(conditions),
                    "by_model": self._by_model(project_id, conditions),
                    "by_agent": self._by_agent(project_id, conditions, run_scope),
                    "by_skill": self._by_skill(project_id, conditions),
                    "by_evaluation": self._by_evaluation(project_id, conditions),
                    "by_user": self._by_user(conditions),
                    "daily": self._daily(conditions),
                }, 200

            except Exception:
                log.error("Analytics cost query failed", exc_info=True)
                return {"error": "Failed to query analytics costs"}, 500

        # Keys that are summed into the Other row; everything else is left None.
        # Explicit list avoids the Decimal/int isinstance trap and prevents ID columns
        # (root_entity_id, root_project_id, user_id …) from being accidentally summed.
        _COLLAPSE_SUM_KEYS = (
            "calls", "eval_runs",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "total_tokens",
            "cost_nano",
            "input_cost_nano", "output_cost_nano",
            "cache_read_cost_nano", "cache_creation_cost_nano",
            "total_cost_sub_nano", "input_cost_sub_nano", "output_cost_sub_nano",
            "cache_read_cost_sub_nano", "cache_creation_cost_sub_nano",
        )

        @staticmethod
        def _collapse_tail(rows, display_limit):
            """Return rows unchanged when at or under the limit.

            Otherwise keep the top display_limit rows and append one synthetic
            "Other" row with is_other=True whose numeric fields are the sum of
            the dropped tail.  Non-summed fields (IDs, names, emails) are None.
            """
            if len(rows) <= display_limit:
                return rows
            top = rows[:display_limit]
            tail = rows[display_limit:]
            other = {k: None for k in tail[0]}
            for k in PromptLibAPI._COLLAPSE_SUM_KEYS:
                if k in other:
                    other[k] = sum(int(r.get(k) or 0) for r in tail)
            other["is_other"] = True
            other["below_resolution"] = {}
            return top + [other]

        @staticmethod
        def _kpis(conditions):
            """One scan for every headline number.

            Evaluation spend (#6678) is a subset of total_cost, not an addition to it.
            """
            evaluation = an.is_evaluation_row()
            columns = [
                func.sum(case(
                    (evaluation, func.coalesce(UsageEvent.cost_nano_usd, 0)), else_=0,
                )).label("evaluation_cost_nano"),
                an.count_where(evaluation).label("evaluation_calls"),
                an.count_where(and_(
                    evaluation,
                    func.coalesce(UsageEvent.cost_source, "unpriced") != "unpriced",
                    func.coalesce(UsageEvent.cost_nano_usd, 0) == 0,
                    an.total_tokens_expr() > 0,
                )).label("evaluation_sub_nano"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("total_input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("total_output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("total_cache_read_tokens"),
                func.sum(
                    func.coalesce(UsageEvent.cache_creation_tokens, 0)
                ).label("total_cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.count().label("total_calls"),
            ] + an.cost_split_sums() + an.below_resolution_sums()

            # An ungrouped aggregate always returns exactly one row; the fallback is belt-and-braces.
            row = an.fetch_one(select(*columns).where(*conditions)) or {}

            total_cost = an.cost_usd(row.get("cost_nano"))
            total_calls = row.get("total_calls") or 0

            below_resolution = an.below_resolution(row, prefix="total_")
            if int(row.get("evaluation_sub_nano") or 0) > 0:
                below_resolution["total_evaluation_cost"] = True

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
                "total_evaluation_cost": an.cost_usd(row.get("evaluation_cost_nano")),
                "evaluation_calls": int(row.get("evaluation_calls") or 0),
                "below_resolution": below_resolution,
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
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ] + an.cost_split_sums() + an.below_resolution_sums()

            statement = select(*columns).where(
                *conditions, UsageEvent.model_name.isnot(None), UsageEvent.model_name != "",
            ).group_by(UsageEvent.model_name).order_by(
                func.sum(UsageEvent.cost_nano_usd).desc(),
            )

            rows = an.fetch_all(statement)
            rows = PromptLibAPI._collapse_tail(rows, _MODEL_DISPLAY)
            display_names = an.model_display_names(project_id) if rows else {}

            return [
                {
                    "model_name": r["model_name"],
                    "display_name": (
                        "Other models"
                        if r.get("is_other")
                        else display_names.get(r["model_name"], r["model_name"])
                    ),
                    "calls": r["calls"],
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "total_cost": an.cost_usd(r["cost_nano"]),
                    **an.cost_split_usd(r),
                    "below_resolution": an.below_resolution(r),
                }
                for r in rows
            ]

        @staticmethod
        def _by_agent(project_id, conditions, run_scope=None):
            """Cost/token breakdown per agent run.

            Grouped by (root_entity_type, root_entity_id): every llm row already carries the
            root of the run it belongs to, so this reads straight off the row instead of the
            elitea_core original's trace_id join to a separate application-type event.

            The label is the application's own name from elitea_core, falling back to the run
            row's stored name without its version suffix (#6910): rows span every version.

            entity_kind (agent/pipeline) comes from elitea_core: root_entity_type is always
            'application' on the row, pipelines included (#6678). The root's own project is
            part of the key, since a public agent's id belongs to the public project (#6902).
            """
            root_name = an.run_name_expr()
            root_project = an.root_project_expr()
            columns = [
                UsageEvent.root_entity_id,
                root_project.label("root_project_id"),
                root_name.label("entity_name"),
                func.count().label("calls"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_creation_tokens, 0)).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ] + an.cost_split_sums() + an.below_resolution_sums()

            statement = select(*columns).where(
                *conditions, an.is_agent_row(run_scope), UsageEvent.root_entity_id.isnot(None),
            ).group_by(
                UsageEvent.root_entity_type, UsageEvent.root_entity_id, root_project,
            ).order_by(func.sum(UsageEvent.cost_nano_usd).desc())

            rows = an.fetch_all(statement)
            rows = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY)
            applications = an.application_meta(
                project_id, refs=[
                    (r["root_project_id"], r["root_entity_id"]) for r in rows
                    if not r.get("is_other")
                ],
            )

            return [
                {
                    "entity_name": (
                        "Other agents"
                        if r.get("is_other")
                        else (
                            applications.get((r["root_project_id"], r["root_entity_id"]), {}).get("name")
                            or r["entity_name"]
                            or f"Agent #{r['root_entity_id']}"
                        )
                    ),
                    "entity_id": None if r.get("is_other") else r["root_entity_id"],
                    "entity_kind": None if r.get("is_other") else applications.get((r["root_project_id"], r["root_entity_id"]), {}).get("kind"),
                    "total_cost": an.cost_usd(r["cost_nano"]),
                    **an.cost_split_usd(r),
                    "below_resolution": an.below_resolution(r),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "calls": r["calls"] or 0,
                    "avg_cost": (
                        round(an.cost_usd(r["cost_nano"]) / r["calls"], 9)
                        if r["cost_nano"] and r["calls"] else 0.0
                    ),
                }
                for r in rows
            ]

        @staticmethod
        def _by_skill(project_id, conditions):
            root_project = an.root_project_expr()
            columns = [
                UsageEvent.root_entity_id,
                root_project.label("root_project_id"),
                an.run_name_expr().label("skill_name"),
                func.count().label("calls"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_creation_tokens, 0)).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ] + an.cost_split_sums() + an.below_resolution_sums()

            statement = select(*columns).where(
                *conditions, an.is_skill_run_row(),
            ).group_by(
                UsageEvent.root_entity_id, root_project,
            ).order_by(func.sum(UsageEvent.cost_nano_usd).desc())

            rows = PromptLibAPI._collapse_tail(an.fetch_all(statement), _SKILL_DISPLAY)
            skills, _ = sa.fetch_skill_meta(project_id, [
                (r["root_project_id"], r["root_entity_id"]) for r in rows if not r.get("is_other")
            ])

            return [
                {
                    **PromptLibAPI._skill_identity(r, skills),
                    "total_cost": an.cost_usd(r["cost_nano"]),
                    **an.cost_split_usd(r),
                    "below_resolution": an.below_resolution(r),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "calls": r["calls"] or 0,
                }
                for r in rows
            ]

        @staticmethod
        def _skill_identity(row, skills):
            if row.get("is_other"):
                return {
                    "skill_id": None, "skill_project_id": None, "skill_name": "Other skills",
                    "deleted": False, "is_other": True,
                }
            return sa.identify_skill(row["root_entity_id"], row["root_project_id"], row["skill_name"], skills)

        @staticmethod
        def _by_evaluation(project_id, conditions):
            """Evaluation spend (#6677) per evaluated agent/pipeline version.

            Evaluation rows name the eval run as their leaf and the evaluated application as
            their root, with no names on either, so labels and kind come from elitea_core.
            """
            columns = [
                UsageEvent.root_entity_id,
                UsageEvent.root_entity_version_id,
                func.count(distinct(UsageEvent.entity_id)).label("eval_runs"),
                func.count().label("calls"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_creation_tokens, 0)).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ] + an.cost_split_sums() + an.below_resolution_sums()

            statement = select(*columns).where(
                *conditions, an.is_evaluation_row(), UsageEvent.root_entity_id.isnot(None),
            ).group_by(
                UsageEvent.root_entity_id, UsageEvent.root_entity_version_id,
            ).order_by(func.sum(UsageEvent.cost_nano_usd).desc())

            rows = an.fetch_all(statement)
            rows = PromptLibAPI._collapse_tail(rows, _AGENT_DISPLAY)
            real_rows = [r for r in rows if not r.get("is_other")]
            applications, versions = an.entity_meta(
                project_id,
                application_ids=[r["root_entity_id"] for r in real_rows],
                version_ids=[r["root_entity_version_id"] for r in real_rows],
            )

            result = []
            for r in rows:
                if r.get("is_other"):
                    result.append({
                        "entity_name": "Other agents",
                        "entity_id": None,
                        "entity_kind": None,
                        "version_id": None,
                        "version_name": None,
                        "eval_runs": int(r.get("eval_runs") or 0),
                        "total_cost": an.cost_usd(r["cost_nano"]),
                        **an.cost_split_usd(r),
                        "below_resolution": an.below_resolution(r),
                        "input_tokens": int(r.get("input_tokens") or 0),
                        "output_tokens": int(r.get("output_tokens") or 0),
                        "cache_read_tokens": int(r.get("cache_read_tokens") or 0),
                        "cache_creation_tokens": int(r.get("cache_creation_tokens") or 0),
                        "total_tokens": int(r.get("total_tokens") or 0),
                        "calls": int(r.get("calls") or 0),
                        "avg_cost": (
                            round(an.cost_usd(r["cost_nano"]) / r["calls"], 9)
                            if r.get("cost_nano") and r.get("calls") else 0.0
                        ),
                    })
                    continue
                application = applications.get(r["root_entity_id"], {})
                version = versions.get(r["root_entity_version_id"], {})
                result.append({
                    "entity_name": application.get("name") or f"Agent #{r['root_entity_id']}",
                    "entity_id": r["root_entity_id"],
                    "entity_kind": version.get("kind") or application.get("kind"),
                    "version_id": r["root_entity_version_id"],
                    "version_name": version.get("name"),
                    "eval_runs": r["eval_runs"] or 0,
                    "total_cost": an.cost_usd(r["cost_nano"]),
                    **an.cost_split_usd(r),
                    "below_resolution": an.below_resolution(r),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "calls": r["calls"] or 0,
                    "avg_cost": (
                        round(an.cost_usd(r["cost_nano"]) / r["calls"], 9)
                        if r["cost_nano"] and r["calls"] else 0.0
                    ),
                })
            return result

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
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ] + an.cost_split_sums() + an.below_resolution_sums()

            statement = select(*columns).where(*conditions).group_by(
                UsageEvent.user_id,
            ).order_by(func.sum(UsageEvent.cost_nano_usd).desc())

            rows = an.fetch_all(statement)
            rows = PromptLibAPI._collapse_tail(rows, _USER_DISPLAY)

            emails = an.label_users(
                [r["user_id"] for r in rows if not r.get("is_other")],
                {r["user_id"]: r["user_email"] for r in rows if not r.get("is_other") and r["user_id"] is not None},
            )

            return [
                {
                    "user_id": None if r.get("is_other") else r["user_id"],
                    "user_email": (
                        "Other users"
                        if r.get("is_other")
                        else emails.get(r["user_id"])
                    ),
                    "total_cost": an.cost_usd(r["cost_nano"]),
                    **an.cost_split_usd(r),
                    "below_resolution": an.below_resolution(r),
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
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ] + an.cost_split_sums() + an.below_resolution_sums()

            statement = select(*columns).where(*conditions).group_by(day).order_by(day)

            rows = an.fetch_all(statement)

            return [
                {
                    "date": r["day"].isoformat() if r["day"] else None,
                    "total_cost": an.cost_usd(r["cost_nano"]),
                    **an.cost_split_usd(r),
                    "below_resolution": an.below_resolution(r),
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

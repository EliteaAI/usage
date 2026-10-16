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
    from sqlalchemy import case, desc, distinct, func, nulls_last, or_, select

    from ...methods import _analytics as an
    from ...methods import _skill_analytics as sa
    from ...methods import _version_names as vn
    from ...models.usage_event import UsageEvent

    _USERS_LIMIT = 50
    _TOOLS_LIMIT = 30
    _SKILLS_LIMIT = 30

    class PromptLibAPI(api_tools.APIModeHandler):
        """Full breakdown for one agent/pipeline."""

        @register_openapi(
            name="Get Agent Analytics Detail",
            description=(
                "Returns the full usage breakdown for one agent/pipeline: KPIs, its users, "
                "the tools it called, the skills it used, its daily activity trend, and its runs, tokens "
                "and cost by version, by model (LLM calls only) and by trigger source (one trigger per run; "
                "manual is a UI or API run). Evaluation tokens and spend "
                "are reported separately under evaluation (null for a single run) and are not "
                "part of kpis."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need the full drill-down for one specific agent or pipeline: its KPIs, which users ran it, which tools it called, which skills it used, its daily trend, and its version, model and trigger-source breakdowns. For its individual runs use List Agent Runs. Do not use this tool for a leaderboard across agents — use List Agent Analytics. Do not use for project-wide KPIs — use Get Project AI Analytics.",
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
                *an.RUN_SCOPE_PARAMETERS,
            ],
            responses={
                "200": {
                    "description": "Agent analytics detail",
                    "content": {
                        "application/json": {
                            "example": {
                                "entity_name": "Code Review Bot",
                                "entity_id": 7,
                                "kind": "agent",
                                "entity_project_id": 1,
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
                                "evaluation": {
                                    "runs": 2,
                                    "input_tokens": 12000,
                                    "output_tokens": 3400,
                                    "cache_read_tokens": 0,
                                    "cache_creation_tokens": 0,
                                    "total_tokens": 15400,
                                    "cost": 0.00154,
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
                                "skills_total": 1,
                                "skills_truncated": False,
                                "skills": [
                                    {
                                        "skill_id": 174,
                                        "skill_project_id": 1,
                                        "skill_name": "summarizer",
                                        "deleted": False,
                                        "uses": 9,
                                        "runs": 6,
                                    },
                                ],
                                "daily_usage": [
                                    {"date": "2025-01-01", "events": 20, "errors": 1},
                                ],
                                "by_version": [
                                    {
                                        "version_id": 31,
                                        "version_name": "base",
                                        "runs": 4,
                                        "total_tokens": 1200,
                                        "cost": 0.0123,
                                    },
                                ],
                                "by_model": [
                                    {
                                        "model_name": "gpt-4o",
                                        "display_name": "GPT-4o",
                                        "runs": 3,
                                        "calls": 7,
                                        "total_tokens": 1100,
                                        "cost": 0.011,
                                    },
                                ],
                                "by_trigger_source": [
                                    {"trigger_source": "manual", "runs": 3, "total_tokens": 900, "cost": 0.009},
                                    {"trigger_source": "scheduled", "runs": 1, "total_tokens": 300, "cost": 0.003},
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

                run_scope, error = an.request_run_scope(project_id, request.args)
                if error:
                    return error
                dt_from, dt_to = an.parse_date_range(request.args, run_scope=run_scope)
                conditions = an.base_filters(project_id, dt_from, dt_to, run_scope=run_scope) + [
                    an.is_agent_row(run_scope),
                    UsageEvent.root_entity_id == entity_id,
                ]

                entity_name, kpis = self._kpis(conditions, entity_id, run_scope)
                if kpis["total_events"] == 0:
                    return {"error": "Agent not found in the selected range"}, 404

                users_total, users_truncated, users = self._users(conditions, run_scope)
                skills_total, skills_truncated, skills = self._skills(
                    project_id, self._skill_conditions(project_id, entity_id, dt_from, dt_to, run_scope),
                )

                evaluation = None if run_scope else self._evaluation(
                    an.base_filters(project_id, dt_from, dt_to) + [
                        an.is_evaluation_row(),
                        UsageEvent.root_entity_id == entity_id,
                    ]
                )

                entity_project_id, kind = self._resolve_entity_identity(project_id, conditions, entity_id)
                return {
                    "entity_name": entity_name,
                    "entity_id": entity_id,
                    "kind": kind,
                    "entity_project_id": entity_project_id,
                    "kpis": kpis,
                    "evaluation": evaluation,
                    "users_total": users_total,
                    "users_truncated": users_truncated,
                    "users": users,
                    "tools": self._tools(conditions),
                    "skills_total": skills_total,
                    "skills_truncated": skills_truncated,
                    "skills": skills,
                    "daily_usage": self._daily_usage(conditions, run_scope),
                    "by_version": self._fetch_version_breakdown(project_id, conditions, run_scope),
                    "by_model": self._fetch_model_breakdown(project_id, conditions),
                    "by_trigger_source": self._fetch_trigger_breakdown(conditions),
                }, 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics agent detail query failed", exc_info=True)
                return {"error": "Failed to query analytics agent detail"}, 500

        @staticmethod
        def _kpis(conditions, entity_id, run_scope=None):
            """Display name plus the headline numbers, in one scan."""
            # No root_entity_name column: read the name off whichever row IS the run
            # (an.is_run_row()).
            entity_name_expr = func.max(case(
                (an.is_run_row(), UsageEvent.entity_name),
                else_=None,
            ))

            columns = [
                entity_name_expr.label("entity_name"),
                an.agent_runs_expr(run_scope).label("total_events"),
                func.count(distinct(UsageEvent.user_id)).label("unique_users"),
                func.avg(UsageEvent.duration_ms).label("avg_duration_ms"),
                # Failed runs, so error_rate stays a percentage of total_events (also runs)
                an.agent_error_runs_expr(run_scope).label("errors"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(
                    func.coalesce(UsageEvent.cache_creation_tokens, 0)
                ).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                # No error-row exclusion (D1): a provider that charged for a failed call still
                # spent the tokens.
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
                an.llm_calls_expr().label("llm_calls"),
            ] + an.cost_split_sums()

            stmt = select(*columns).where(*conditions)

            row = an.fetch_one(stmt)
            #
            total_events = int(row["total_events"] or 0) if row else 0
            errors = int(row["errors"] or 0) if row else 0
            llm_calls = int(row["llm_calls"] or 0) if row else 0
            llm_cost = an.cost_usd(row["cost_nano"] if row else 0)
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
                **an.cost_split_usd(row or {}),
                "avg_cost_per_call": round(llm_cost / llm_calls, 6) if llm_calls else 0,
            }
            #
            return entity_name, kpis

        @staticmethod
        def _resolve_entity_identity(project_id, conditions, entity_id):
            row = an.fetch_one(select(func.max(an.root_project_expr()).label("entity_project_id")).where(*conditions))
            entity_project_id = (row or {}).get("entity_project_id")
            application = an.application_meta(project_id, refs=[(entity_project_id, entity_id)])
            return entity_project_id, application.get((entity_project_id, entity_id), {}).get("kind")

        @staticmethod
        def _fetch_version_breakdown(project_id, conditions, run_scope=None):
            version_id = UsageEvent.root_entity_version_id.label("version_id")
            runs_col = an.agent_runs_expr(run_scope).label("runs")
            rows = an.fetch_all(select(
                version_id,
                an.pick_run_row_name().label("stored_name"),
                func.max(an.root_project_expr()).label("root_project_id"),
                func.max(UsageEvent.root_entity_type).label("root_entity_type"),
                func.max(UsageEvent.root_entity_id).label("root_entity_id"),
                runs_col,
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ).where(*conditions).group_by(UsageEvent.root_entity_version_id).order_by(
                desc(runs_col), nulls_last(UsageEvent.root_entity_version_id),
            ))
            version_names = vn.label_versions(project_id, rows)
            return [
                {
                    "version_id": r["version_id"],
                    "version_name": version_names.get(vn.build_version_key(project_id, r)),
                    "runs": int(r["runs"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "cost": an.cost_usd(r["cost_nano"]),
                }
                for r in rows
            ]

        @staticmethod
        def _fetch_model_breakdown(project_id, conditions):
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            rows = an.fetch_all(select(
                UsageEvent.model_name,
                func.count(distinct(UsageEvent.run_id)).label("runs"),
                func.count().label("calls"),
                total_tokens_col,
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ).where(*conditions, UsageEvent.event_type == an.EVENT_LLM).group_by(UsageEvent.model_name).order_by(
                desc(total_tokens_col), nulls_last(UsageEvent.model_name),
            ))
            display_names = an.model_display_names(project_id) if rows else {}
            return sa.format_model_breakdown(rows, display_names)

        @staticmethod
        def _fetch_trigger_breakdown(conditions):
            per_run = select(
                UsageEvent.run_id,
                func.max(UsageEvent.trigger_source).label("trigger_source"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ).where(*conditions).group_by(UsageEvent.run_id).subquery("per_run")
            runs_col = func.count(per_run.c.run_id).label("runs")
            rows = an.fetch_all(select(
                per_run.c.trigger_source,
                runs_col,
                func.sum(per_run.c.total_tokens).label("total_tokens"),
                func.sum(per_run.c.cost_nano).label("cost_nano"),
            ).group_by(per_run.c.trigger_source).order_by(desc(runs_col), nulls_last(per_run.c.trigger_source)))
            return [
                {
                    "trigger_source": r["trigger_source"] or sa.TRIGGER_MANUAL,
                    "runs": int(r["runs"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "cost": an.cost_usd(r["cost_nano"]),
                }
                for r in rows
            ]

        @staticmethod
        def _evaluation(conditions):
            """Tokens and spend of evaluations run against this agent (#6678), kept out of kpis."""
            columns = [
                func.count(distinct(UsageEvent.entity_id)).label("runs"),
                func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
                func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
                func.sum(func.coalesce(UsageEvent.cache_read_tokens, 0)).label("cache_read_tokens"),
                func.sum(
                    func.coalesce(UsageEvent.cache_creation_tokens, 0)
                ).label("cache_creation_tokens"),
                func.sum(an.total_tokens_expr()).label("total_tokens"),
                func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano"),
            ]
            row = an.fetch_one(select(*columns).where(*conditions)) or {}
            return {
                "runs": int(row.get("runs") or 0),
                "input_tokens": int(row.get("input_tokens") or 0),
                "output_tokens": int(row.get("output_tokens") or 0),
                "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
                "cache_creation_tokens": int(row.get("cache_creation_tokens") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
                "cost": an.cost_usd(row.get("cost_nano")),
            }

        @staticmethod
        def _users(conditions, run_scope=None):
            """Per-user breakdown, grouped by user_id alone.

            user_email is null on rows written before the write path populated it, and
            grouping by the (user_id, user_email) pair would split one person into several
            rows whenever both a null and a populated email exist for them.
            """
            events_col = an.agent_runs_expr(run_scope).label("events")
            #
            rows = an.fetch_all(select(
                UsageEvent.user_id,
                func.max(UsageEvent.user_email).label("user_email"),
                events_col,
                func.avg(UsageEvent.duration_ms).label("avg_duration_ms"),
                an.agent_error_runs_expr(run_scope).label("errors"),
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
        def _skill_conditions(project_id, entity_id, dt_from, dt_to, run_scope=None):
            conditions = an.base_filters(
                project_id, dt_from, dt_to, run_scope=run_scope, event_types=(an.EVENT_SKILL,),
            ) + [
                an.is_agent_row(run_scope),
                UsageEvent.root_entity_id == entity_id,
                UsageEvent.entity_id.isnot(None),
            ]
            if run_scope is None or run_scope.eval_run_id is None:
                conditions.append(or_(
                    UsageEvent.run_id.is_(None),
                    UsageEvent.run_id.notin_(
                        PromptLibAPI._evaluation_run_ids(project_id, entity_id, dt_from, dt_to),
                    ),
                ))
            return conditions

        @staticmethod
        def _evaluation_run_ids(project_id, entity_id, dt_from, dt_to):
            return select(UsageEvent.run_id).where(
                *an.base_filters(project_id, dt_from, dt_to, human_only=False, event_types=None),
                UsageEvent.root_entity_id == entity_id,
                an.is_evaluation_row(),
                UsageEvent.run_id.isnot(None),
            ).correlate(None)

        @staticmethod
        def _skills(project_id, conditions):
            skill_project = sa.resolve_skill_project_id(UsageEvent)
            uses_col = func.count().label("uses")
            grouped = select(
                UsageEvent.entity_id,
                skill_project.label("skill_project_id"),
                func.max(UsageEvent.entity_name).label("skill_name"),
                uses_col,
                func.count(distinct(UsageEvent.run_id)).label("runs"),
            ).where(*conditions).group_by(
                UsageEvent.entity_id, skill_project,
            )
            rows = an.fetch_all(grouped.order_by(uses_col.desc()).limit(_SKILLS_LIMIT))
            total_row = an.fetch_one(select(func.count().label("total")).select_from(grouped.subquery()))
            skills_total = (total_row["total"] if total_row else 0) or 0
            skills, _ = sa.fetch_skill_meta(project_id, [(r["skill_project_id"], r["entity_id"]) for r in rows])
            return skills_total, skills_total > len(rows), [
                {
                    **sa.identify_skill(r["entity_id"], r["skill_project_id"], r["skill_name"], skills),
                    "uses": int(r["uses"] or 0),
                    "runs": int(r["runs"] or 0),
                }
                for r in rows
            ]

        @staticmethod
        def _daily_usage(conditions, run_scope=None):
            """Daily run count and errors."""
            day = an.day_expr().label("day")
            events_col = an.agent_runs_expr(run_scope).label("events")
            #
            rows = an.fetch_all(select(
                day,
                events_col,
                an.agent_error_runs_expr(run_scope).label("errors"),
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

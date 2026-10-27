"""
Single-chat analytics detail endpoint.

Returns the full breakdown for one user chat conversation identified by conversation_uuid.
"""

import uuid as _uuid_module

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import Date, cast, desc, distinct, func, nulls_last, select

    from ...methods import _analytics as an
    from ...methods import _skill_analytics as sa
    from ...methods import analytics_chats as ac
    from ...models.usage_event import UsageEvent

    class PromptLibAPI(api_tools.APIModeHandler):
        """Full breakdown for one chat conversation."""

        @register_openapi(
            name="Get Chat Detail",
            description=(
                "Returns the full usage breakdown for one user chat conversation: KPIs, "
                "participant breakdown, model breakdown, user breakdown, tool calls, and "
                "daily activity. Identified by conversation_uuid."
            ),
            mcp_tool=True,
            mcp_description=(
                "Use this tool when you need the full drill-down for one specific user chat: "
                "its KPIs, which participants were involved, which models were called, token and "
                "cost breakdowns. For the paginated list use List Chat Analytics."
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
                    "name": "conversation_uuid",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                    "description": "The conversation UUID (usage_event.conversation_id).",
                    "example": "0b6f3c7a-5d2e-4f8a-b1c9-7e4d2a6f9c30",
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
                    "description": "Chat analytics detail",
                    "content": {
                        "application/json": {
                            "example": {
                                "conversation_uuid": "0b6f3c7a-5d2e-4f8a-b1c9-7e4d2a6f9c30",
                                "chat_name": "Code review chat",
                                "is_private": False,
                                "is_deleted": False,
                                "owner": {"id": 11},
                                "kpis": {
                                    "responses": 8,
                                    "users": 1,
                                    "errors": 0,
                                    "total_tokens": 4200,
                                    "llm_cost": 0.00042,
                                    "first_activity": "2026-01-15T09:00:00+00:00",
                                    "last_activity": "2026-01-15T14:30:00+00:00",
                                },
                                "by_participant": [],
                                "by_model": [],
                                "by_user": [],
                                "tools": [],
                                "daily": [],
                            }
                        }
                    },
                },
                "400": {"description": "conversation_uuid is missing or invalid"},
                "401": {"description": "Unauthorized"},
                "404": {"description": "Chat not found in range"},
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
            GET /api/v2/usage/analytics_chat_detail/prompt_lib/<project_id>
            """
            try:
                raw_uuid = (request.args.get("conversation_uuid") or "").strip()
                if not raw_uuid:
                    return {"error": "conversation_uuid is required"}, 400
                try:
                    conversation_uuid = str(_uuid_module.UUID(raw_uuid))
                except ValueError:
                    return {"error": "conversation_uuid must be a valid UUID"}, 400

                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = ac.chat_base_filters(project_id, dt_from, dt_to)
                conditions.append(UsageEvent.conversation_id == conversation_uuid)

                kpis = self._kpis(conditions)
                if kpis["responses"] == 0:
                    return {"error": "Chat not found in the selected range"}, 404

                # Enrich with metadata from elitea_core
                meta_rows = ac.resolve_chat_meta(
                    project_id, [{"conversation_uuid": conversation_uuid}],
                    requester_id=auth.current_user().get("id"),
                )
                meta = meta_rows[0] if meta_rows else {}

                return {
                    "conversation_uuid": conversation_uuid,
                    "chat_name": meta.get("chat_name"),
                    "is_private": meta.get("is_private", True),
                    "is_deleted": meta.get("is_deleted", False),
                    "meta_unavailable": meta.get("meta_unavailable", False),
                    "can_open": meta.get("can_open", False),
                    "conversation_id": meta.get("conversation_id"),
                    "owner": {"id": meta["owner_id"]} if meta.get("owner_id") is not None else None,
                    "kpis": kpis,
                    "by_participant": self._by_participant(project_id, conditions),
                    "by_model": self._by_model(project_id, conditions),
                    "by_user": self._by_user(conditions),
                    "tools": self._tools(conditions),
                    "daily": self._daily(conditions),
                }, 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics chat detail query failed", exc_info=True)
                return {"error": "Failed to query analytics chat detail"}, 500

        @staticmethod
        def _kpis(conditions):
            """Headline KPIs for one conversation."""
            responses_col = func.count(distinct(UsageEvent.run_id)).label("responses")
            users_col = func.count(distinct(UsageEvent.user_id)).label("users")
            errors_col = ac.chat_error_responses_expr().label("errors")
            first_activity_col = func.min(UsageEvent.ts).label("first_activity")
            last_activity_col = func.max(UsageEvent.ts).label("last_activity")
            avg_dur_col = func.avg(UsageEvent.duration_ms).label("avg_duration_ms")

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

            row = an.fetch_one(select(
                responses_col,
                users_col,
                errors_col,
                first_activity_col,
                last_activity_col,
                avg_dur_col,
                input_tokens_col,
                output_tokens_col,
                cache_read_tokens_col,
                cache_creation_tokens_col,
                total_tokens_col,
                cost_nano_col,
                *an.cost_split_sums(),
                *an.below_resolution_sums(),
            ).where(*conditions)) or {}

            responses = int(row.get("responses") or 0)
            llm_cost = an.cost_usd(row.get("cost_nano") or 0)
            first_activity = row.get("first_activity")
            last_activity = row.get("last_activity")

            return {
                "responses": responses,
                "users": int(row.get("users") or 0),
                "errors": int(row.get("errors") or 0),
                "avg_duration_ms": an.rounded(row.get("avg_duration_ms"), 1),
                "first_activity": first_activity.isoformat() if first_activity else None,
                "last_activity": last_activity.isoformat() if last_activity else None,
                "input_tokens": int(row.get("input_tokens") or 0),
                "output_tokens": int(row.get("output_tokens") or 0),
                "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
                "cache_creation_tokens": int(row.get("cache_creation_tokens") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
                "llm_cost": llm_cost,
                **an.cost_split_usd(row),
                "avg_cost_per_response": round(llm_cost / responses, 9) if responses else 0,
                "below_resolution": an.below_resolution(row),
            }

        @staticmethod
        def _by_participant(project_id, conditions):
            """Responses, tokens, cost per root entity (participant)."""
            responses_col = func.count(distinct(UsageEvent.run_id)).label("responses")
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            cost_nano_col = func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano")

            root_project = an.root_project_expr().label("root_project_id")

            rows = an.fetch_all(
                select(
                    UsageEvent.root_entity_type.label("participant_type"),
                    UsageEvent.root_entity_id.label("participant_id"),
                    root_project,
                    func.max(UsageEvent.entity_name).label("stored_name"),
                    responses_col,
                    total_tokens_col,
                    cost_nano_col,
                )
                .where(*conditions)
                .group_by(UsageEvent.root_entity_type, UsageEvent.root_entity_id, root_project)
                .order_by(desc(responses_col))
            )

            # App and skill ids come from separate sequences and are only unique inside their own
            # schema, so each participant is looked up by its type and the schema it lives in.
            has_id = [r for r in rows if r["participant_id"] is not None]
            app_refs = [
                (r["root_project_id"], r["participant_id"])
                for r in has_id if r["participant_type"] != "skill"
            ]
            skill_refs = [
                (r["root_project_id"], r["participant_id"])
                for r in has_id if r["participant_type"] == "skill"
            ]
            applications = an.application_meta(project_id, refs=app_refs) if app_refs else {}
            skills = sa.fetch_skill_meta(project_id, skill_refs)[0] if skill_refs else {}

            def resolved_name(row):
                key = (row["root_project_id"], row["participant_id"])
                if row["participant_type"] == "skill":
                    return (skills or {}).get(key, {}).get("name")
                return applications.get(key, {}).get("name")

            _TYPE_MAP = {None: "model", "application": "agent", "pipeline": "pipeline", "skill": "skill"}

            return [
                {
                    "participant_type": _TYPE_MAP.get(r["participant_type"], r["participant_type"] or "model"),
                    "participant_id": r["participant_id"],
                    "participant_name": (
                        resolved_name(r)
                        or r["stored_name"]
                        or (f"{r['participant_type']} #{r['participant_id']}" if r["participant_id"] else "model")
                    ),
                    "responses": int(r["responses"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(r["cost_nano"]),
                }
                for r in rows
            ]

        @staticmethod
        def _by_model(project_id, conditions):
            """Responses, calls, tokens, cost per model."""
            model_scope = an.model_scope_expr()
            runs_col = func.count(distinct(UsageEvent.run_id)).label("runs")
            calls_col = an.llm_calls_expr().label("calls")
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            cost_nano_col = func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano")

            rows = an.fetch_all(
                select(
                    UsageEvent.model_name,
                    model_scope.label("model_scope"),
                    runs_col,
                    calls_col,
                    total_tokens_col,
                    cost_nano_col,
                )
                .where(*conditions, UsageEvent.event_type == an.EVENT_LLM)
                .group_by(UsageEvent.model_name, model_scope)
                .order_by(desc(runs_col))
            )

            model_labels = an.model_labels(
                project_id,
                [{"model_name": r["model_name"], "model_scope": r["model_scope"]} for r in rows],
            )

            return [
                {
                    "model_name": r["model_name"],
                    "display_name": model_labels.get(r["model_name"], r["model_scope"]) or r["model_name"],
                    "runs": int(r["runs"] or 0),
                    "calls": int(r["calls"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(r["cost_nano"]),
                }
                for r in rows
            ]

        @staticmethod
        def _by_user(conditions):
            """Responses, tokens, cost per user."""
            user_ids = [r["user_id"] for r in an.fetch_all(
                select(distinct(UsageEvent.user_id).label("user_id")).where(*conditions)
            ) if r["user_id"] is not None]

            responses_col = func.count(distinct(UsageEvent.run_id)).label("responses")
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            cost_nano_col = func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano")

            rows = an.fetch_all(
                select(
                    UsageEvent.user_id,
                    func.max(UsageEvent.user_email).label("user_email"),
                    responses_col,
                    total_tokens_col,
                    cost_nano_col,
                )
                .where(*conditions)
                .group_by(UsageEvent.user_id)
                .order_by(desc(responses_col))
            )

            emails = an.label_users(
                user_ids,
                {r["user_id"]: r["user_email"] for r in rows if r["user_id"] is not None},
            )

            return [
                {
                    "user_id": r["user_id"],
                    "user_email": emails.get(r["user_id"]),
                    "responses": int(r["responses"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(r["cost_nano"]),
                }
                for r in rows
            ]

        @staticmethod
        def _tools(conditions):
            """Tool call counts and errors per tool_name."""
            calls_col = func.count().label("calls")
            errors_col = an.count_where(UsageEvent.is_error.is_(True)).label("errors")

            rows = an.fetch_all(
                select(
                    UsageEvent.tool_name,
                    calls_col,
                    errors_col,
                )
                .where(*conditions, UsageEvent.event_type == an.EVENT_TOOL)
                .group_by(UsageEvent.tool_name)
                .order_by(desc(calls_col))
            )

            return [
                {
                    "tool_name": r["tool_name"],
                    "calls": int(r["calls"] or 0),
                    "errors": int(r["errors"] or 0),
                }
                for r in rows
            ]

        @staticmethod
        def _daily(conditions):
            """Daily responses, tokens and cost."""
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

            return [
                {
                    "date": r["date"].isoformat() if r["date"] else None,
                    "responses": int(r["responses"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(r["cost_nano"]),
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

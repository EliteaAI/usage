"""
Paginated AI responses (runs) for one chat conversation.

Each row represents one run_id: one AI response within the conversation.
Modeled on analytics_agent_runs.py.
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
    from sqlalchemy import asc, case, desc, distinct, func, nulls_last, select

    from ...methods import _analytics as an
    from ...methods import _skill_analytics as sa
    from ...methods import analytics_chats as ac
    from ...models.usage_event import UsageEvent

    _SORT_COLUMNS = ("started_at", "user", "total_tokens", "cost", "status")
    _DEFAULT_SORT = "started_at"

    class PromptLibAPI(api_tools.APIModeHandler):
        """Paginated AI responses (runs) for one chat conversation."""

        @register_openapi(
            name="List Chat Runs",
            description=(
                "Returns the AI responses (runs) for one user chat conversation, newest first "
                "by default. One row per run_id with start time, user, participant type, "
                "participant name, models, tokens, cost and error status."
            ),
            mcp_tool=True,
            mcp_description=(
                "Use this tool when you need the individual AI responses for one specific chat "
                "conversation, paginated and sortable. Do not use for the conversation's aggregate "
                "breakdowns — use Get Chat Detail."
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
                    "name": "sort_by",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "type": "string",
                        "enum": list(_SORT_COLUMNS),
                        "default": _DEFAULT_SORT,
                    },
                    "description": "Column to sort by.",
                },
                {
                    "name": "sort_order",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    "description": "Sort direction.",
                },
            ],
            responses={
                "200": {
                    "description": "Paginated chat runs",
                    "content": {
                        "application/json": {
                            "example": {
                                "total": 8,
                                "rows": [
                                    {
                                        "run_id": "8d0c8f6e-2f1b-4c7e-9a51-3c2b1f0e7a10",
                                        "started_at": "2026-01-15T14:30:00+00:00",
                                        "user_id": 11,
                                        "user_email": "user@example.com",
                                        "participant_type": "agent",
                                        "participant_name": "Code Review Bot",
                                        "models": [{"model_name": "gpt-4o", "display_name": "GPT-4o"}],
                                        "total_tokens": 520,
                                        "llm_cost": 0.000052,
                                        "is_error": False,
                                    },
                                ],
                            }
                        }
                    },
                },
                "400": {"description": "conversation_uuid is missing or invalid"},
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
            GET /api/v2/usage/analytics_chat_runs/prompt_lib/<project_id>
            """
            raw_uuid = (request.args.get("conversation_uuid") or "").strip()
            if not raw_uuid:
                return {"error": "conversation_uuid is required"}, 400
            try:
                conversation_uuid = str(_uuid_module.UUID(raw_uuid))
            except ValueError:
                return {"error": "conversation_uuid must be a valid UUID"}, 400

            limit = an.clamp_int(request.args.get("limit"), 20, minimum=1, maximum=100)
            offset = an.clamp_int(request.args.get("offset"), 0, minimum=0)
            sort_by = request.args.get("sort_by", _DEFAULT_SORT)
            if sort_by not in _SORT_COLUMNS:
                sort_by = _DEFAULT_SORT
            sort_order = request.args.get("sort_order", "desc")

            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = ac.chat_base_filters(project_id, dt_from, dt_to)
                conditions.append(UsageEvent.conversation_id == conversation_uuid)
                conditions.append(UsageEvent.run_id.isnot(None))

                total, rows = self._fetch_runs_page(
                    project_id, conditions, sort_by, sort_order, limit, offset,
                )
            except Exception:  # pylint: disable=W0703
                log.error("Analytics chat runs query failed", exc_info=True)
                return {"error": "Failed to query analytics chat runs"}, 500

            return {"total": total, "rows": rows}, 200

        @staticmethod
        def _fetch_runs_page(project_id, conditions, sort_by, sort_order, limit, offset):
            started_col = func.min(UsageEvent.ts).label("started_at")
            user_email_col = func.max(UsageEvent.user_email).label("user_email")
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            cost_nano_col = func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano")
            is_error_col = func.max(case((UsageEvent.is_error.is_(True), 1), else_=0)).label("is_error")

            per_run = select(
                UsageEvent.run_id,
                started_col,
                func.max(UsageEvent.user_id).label("user_id"),
                user_email_col,
                func.max(UsageEvent.root_entity_type).label("participant_type"),
                func.max(UsageEvent.root_entity_id).label("participant_id"),
                func.max(an.root_project_expr()).label("root_project_id"),
                total_tokens_col,
                cost_nano_col,
                is_error_col,
            ).where(*conditions).group_by(UsageEvent.run_id)

            total = sa.count_grouped(per_run)

            sort_column = {
                "started_at": started_col,
                "user": user_email_col,
                "total_tokens": total_tokens_col,
                "cost": cost_nano_col,
                "status": is_error_col,
            }[sort_by]
            order = desc if sort_order == "desc" else asc
            page = an.fetch_all(
                per_run.order_by(
                    nulls_last(order(sort_column)), UsageEvent.run_id,
                ).limit(limit).offset(offset)
            )

            # Resolve emails
            user_ids = [r["user_id"] for r in page if r.get("user_id") is not None]
            emails = an.label_users(
                user_ids,
                {r["user_id"]: r["user_email"] for r in page if r.get("user_id") is not None},
            )

            # Resolve participant names
            with_id = [r for r in page if r.get("participant_id") is not None]
            app_refs = [
                (r["root_project_id"], r["participant_id"])
                for r in with_id if r["participant_type"] != "skill"
            ]
            skill_refs = [
                (r["root_project_id"], r["participant_id"])
                for r in with_id if r["participant_type"] == "skill"
            ]
            applications = an.application_meta(project_id, refs=app_refs) if app_refs else {}
            skills = sa.fetch_skill_meta(project_id, skill_refs)[0] if skill_refs else {}

            def resolved_name(row):
                key = (row["root_project_id"], row["participant_id"])
                if row["participant_type"] == "skill":
                    return (skills or {}).get(key, {}).get("name")
                return applications.get(key, {}).get("name")

            # Resolve models per run
            run_ids = [r["run_id"] for r in page]
            models_by_run = _fetch_models_by_run(project_id, conditions, run_ids)

            _TYPE_MAP = {None: "model", "application": "agent", "pipeline": "pipeline", "skill": "skill"}

            return total, [
                {
                    "run_id": r["run_id"],
                    "started_at": sa.format_iso(r["started_at"]),
                    "user_id": r["user_id"],
                    "user_email": emails.get(r["user_id"]),
                    "participant_type": _TYPE_MAP.get(r["participant_type"], r["participant_type"] or "model"),
                    "participant_name": (
                        resolved_name(r)
                        or (
                            f"{r['participant_type']} #{r['participant_id']}"
                            if r.get("participant_id") else None
                        )
                    ),
                    "models": models_by_run.get(r["run_id"], []),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(r["cost_nano"]),
                    "is_error": bool(r["is_error"]),
                }
                for r in page
            ]


    def _fetch_models_by_run(project_id, conditions, run_ids):
        if not run_ids:
            return {}
        model_scope = an.model_scope_expr()
        rows = an.fetch_all(
            select(
                UsageEvent.run_id,
                UsageEvent.model_name,
                model_scope.label("model_scope"),
            )
            .where(*conditions, UsageEvent.event_type == an.EVENT_LLM, UsageEvent.run_id.in_(run_ids))
            .group_by(UsageEvent.run_id, UsageEvent.model_name, model_scope)
        )
        model_labels = an.model_labels(
            project_id,
            [{"model_name": r["model_name"], "model_scope": r["model_scope"]} for r in rows],
        )
        models = {}
        for r in rows:
            models.setdefault(r["run_id"], []).append({
                "model_name": r["model_name"],
                "display_name": sa.label_model(r["model_name"], model_labels, r["model_scope"]),
            })
        for run_models in models.values():
            run_models.sort(key=lambda m: (m["display_name"] is None, m["display_name"] or ""))
        return models


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

"""
Paginated runs of one agent/pipeline (#7033): one row per usage_event.run_id.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import asc, case, desc, func, nulls_last, select

    from ...methods import _analytics as an
    from ...methods import _skill_analytics as sa
    from ...methods import _version_names as vn
    from ...models.usage_event import UsageEvent

    _SORT_COLUMNS = ("started_at", "user", "version", "model", "total_tokens", "cost", "status")
    _DEFAULT_SORT = "started_at"

    class PromptLibAPI(api_tools.APIModeHandler):
        """Paginated runs of one agent/pipeline."""

        @register_openapi(
            name="List Agent Runs",
            description=(
                "Returns the runs of one agent/pipeline, newest first by default: start time, user, "
                "version, every model the run called, tokens, cost, whether any call failed, and the "
                "Run History conversation it belongs to. Sorted on the server before paging."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need the individual runs of one specific agent or pipeline, paginated and sortable, with each run's user, version, models, tokens, cost and error status. Do not use this tool for the agent's aggregate breakdowns — use Get Agent Analytics Detail. Do not use for a leaderboard across agents — use List Agent Analytics.",
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
                    "schema": {"type": "string", "enum": list(_SORT_COLUMNS), "default": _DEFAULT_SORT},
                    "description": "Column to sort by. An unknown value sorts by started_at.",
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
                    "description": "Paginated agent runs",
                    "content": {
                        "application/json": {
                            "example": {
                                "total": 126,
                                "rows": [
                                    {
                                        "run_id": "8d0c8f6e-2f1b-4c7e-9a51-3c2b1f0e7a10",
                                        "started_at": "2026-10-15T09:12:03+00:00",
                                        "user_id": 11,
                                        "user_email": "user@example.com",
                                        "version_id": 31,
                                        "version_name": "base",
                                        "models": [{"model_name": "gpt-4o", "display_name": "GPT-4o"}],
                                        "total_tokens": 1200,
                                        "cost": 0.0123,
                                        "is_error": False,
                                        "conversation_id": 1701,
                                        "conversation_uuid": "0b6f3c7a-5d2e-4f8a-b1c9-7e4d2a6f9c30",
                                        "root_project_id": 1,
                                    },
                                ],
                            }
                        }
                    },
                },
                "400": {"description": "entity_id is missing or not an integer"},
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
            GET /api/v2/usage/analytics_agent_runs/prompt_lib/<project_id>
            """
            entity_id = an.clamp_int(request.args.get("entity_id"), None)
            if entity_id is None:
                return {"error": "entity_id is required"}, 400
            limit = an.clamp_int(request.args.get("limit"), 20, minimum=1, maximum=100)
            offset = an.clamp_int(request.args.get("offset"), 0, minimum=0)
            sort_by = request.args.get("sort_by", _DEFAULT_SORT)
            if sort_by not in _SORT_COLUMNS:
                sort_by = _DEFAULT_SORT
            sort_order = request.args.get("sort_order", "desc")
            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = an.base_filters(project_id, dt_from, dt_to) + [
                    an.is_agent_row(),
                    UsageEvent.root_entity_id == entity_id,
                    UsageEvent.run_id.isnot(None),
                ]
                total, rows = self._fetch_runs_page(project_id, conditions, sort_by, sort_order, limit, offset)
            except Exception:  # pylint: disable=W0703
                log.error("Analytics agent runs query failed", exc_info=True)
                return {"error": "Failed to query analytics agent runs"}, 500
            return {"total": total, "rows": rows}, 200

        @staticmethod
        def _fetch_runs_page(project_id, conditions, sort_by, sort_order, limit, offset):
            started_col = func.min(UsageEvent.ts).label("started_at")
            user_email_col = func.max(UsageEvent.user_email).label("user_email")
            version_col = func.max(UsageEvent.root_entity_version_id).label("version_id")
            display_names = an.model_display_names(project_id) if sort_by == "model" else None
            display_model_name = (
                case(display_names, value=UsageEvent.model_name, else_=UsageEvent.model_name)
                if display_names else UsageEvent.model_name
            )
            model_col = func.min(display_model_name)
            total_tokens_col = func.sum(an.total_tokens_expr()).label("total_tokens")
            cost_nano_col = func.sum(func.coalesce(UsageEvent.cost_nano_usd, 0)).label("cost_nano")
            is_error_col = func.max(case((UsageEvent.is_error.is_(True), 1), else_=0)).label("is_error")
            per_run = select(
                UsageEvent.run_id,
                started_col,
                func.max(UsageEvent.user_id).label("user_id"),
                user_email_col,
                version_col,
                an.pick_run_row_name().label("stored_name"),
                func.max(an.root_project_expr()).label("root_project_id"),
                func.max(UsageEvent.root_entity_type).label("root_entity_type"),
                func.max(UsageEvent.root_entity_id).label("root_entity_id"),
                total_tokens_col,
                cost_nano_col,
                is_error_col,
                func.max(UsageEvent.conversation_id).label("conversation_uuid"),
            ).where(*conditions).group_by(UsageEvent.run_id)

            total = sa.count_grouped(per_run)
            sort_column = {
                "started_at": started_col,
                "user": user_email_col,
                "version": version_col,
                "model": model_col,
                "total_tokens": total_tokens_col,
                "cost": cost_nano_col,
                "status": is_error_col,
            }[sort_by]
            order = desc if sort_order == "desc" else asc
            page = an.fetch_all(per_run.order_by(
                nulls_last(order(sort_column)), UsageEvent.run_id,
            ).limit(limit).offset(offset))

            if display_names is None:
                display_names = an.model_display_names(project_id) if page else {}
            models = PromptLibAPI._fetch_models_by_run(conditions, [r["run_id"] for r in page], display_names)
            version_names = vn.label_versions(project_id, page)
            emails = an.label_users(
                [r["user_id"] for r in page],
                {r["user_id"]: r["user_email"] for r in page if r["user_id"] is not None},
            )
            _, conversations = sa.fetch_skill_meta(
                project_id, conversation_uuids=[r["conversation_uuid"] for r in page],
            )
            return total, [
                {
                    "run_id": r["run_id"],
                    "started_at": sa.format_iso(r["started_at"]),
                    "user_id": r["user_id"],
                    "user_email": emails.get(r["user_id"]),
                    "version_id": r["version_id"],
                    "version_name": version_names.get(vn.build_version_key(project_id, r)),
                    "models": models.get(r["run_id"], []),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "cost": an.cost_usd(r["cost_nano"]),
                    "is_error": bool(r["is_error"]),
                    "conversation_id": conversations.get(r["conversation_uuid"]),
                    "conversation_uuid": r["conversation_uuid"],
                    "root_project_id": r["root_project_id"],
                }
                for r in page
            ]

        @staticmethod
        def _fetch_models_by_run(conditions, run_ids, display_names):
            if not run_ids:
                return {}
            rows = an.fetch_all(select(UsageEvent.run_id, UsageEvent.model_name).where(
                *conditions, UsageEvent.event_type == an.EVENT_LLM, UsageEvent.run_id.in_(run_ids),
            ).group_by(UsageEvent.run_id, UsageEvent.model_name))
            models = {}
            for r in rows:
                models.setdefault(r["run_id"], []).append({
                    "model_name": r["model_name"],
                    "display_name": sa.label_model(r["model_name"], display_names),
                })
            for run_models in models.values():
                run_models.sort(key=lambda model: (model["display_name"] is None, model["display_name"] or ""))
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

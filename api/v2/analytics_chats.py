"""
Paginated analytics chats endpoint.

Each row represents one user chat conversation grouped by conversation_id.
Names are resolved from elitea_core via RPC since they live outside usage_event.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import asc, case, desc, distinct, func, select

    from ...methods import _analytics as an
    from ...methods import analytics_chats as ac
    from ...models.usage_event import UsageEvent

    _SORT_WHITELIST = frozenset([
        "llm_cost", "total_tokens", "responses", "users",
        "input_cost", "output_cost", "cache_read_cost", "cache_creation_cost",
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "avg_cost_per_response", "avg_duration_ms", "errors", "last_activity", "chat_name",
    ])

    class PromptLibAPI(api_tools.APIModeHandler):
        """Paginated chat conversation usage for analytics."""

        @register_openapi(
            name="List Chat Analytics",
            description=(
                "Returns paginated user chat conversation statistics with optional "
                "date filtering, sorting, and response/user/error counts per conversation."
            ),
            mcp_tool=True,
            mcp_description=(
                "Use this tool when you need a paginated list of user chat conversations with "
                "their usage statistics. Do not use for project-wide KPIs — use the summary "
                "endpoint. Do not use for individual conversation detail — use Get Chat Detail."
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
                    "name": "search",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Filter by conversation name (case-insensitive partial match).",
                },
                {
                    "name": "sort_by",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "type": "string",
                        "enum": sorted(_SORT_WHITELIST),
                        "default": "last_activity",
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
                    "description": "Paginated chat analytics",
                    "content": {
                        "application/json": {
                            "example": {
                                "total": 12,
                                "rows": [
                                    {
                                        "conversation_uuid": "0b6f3c7a-5d2e-4f8a-b1c9-7e4d2a6f9c30",
                                        "chat_name": "Code review chat",
                                        "responses": 8,
                                        "users": 1,
                                        "errors": 0,
                                        "total_tokens": 4200,
                                        "llm_cost": 0.00042,
                                        "last_activity": "2026-01-15T14:30:00+00:00",
                                    },
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
            GET /api/v2/usage/analytics_chats/prompt_lib/<project_id>
            """
            try:
                dt_from, dt_to = an.parse_date_range(request.args)
                conditions = ac.chat_base_filters(project_id, dt_from, dt_to)

                limit = an.clamp_int(request.args.get("limit"), 20, minimum=1, maximum=100)
                offset = an.clamp_int(request.args.get("offset"), 0, minimum=0)

                sort_by = request.args.get("sort_by", "last_activity")
                if sort_by not in _SORT_WHITELIST:
                    sort_by = "last_activity"
                sort_order = request.args.get("sort_order", "desc")
                search = (request.args.get("search") or "").strip()

                # Name search: resolve matching UUIDs via RPC since names live in elitea_core
                if search:
                    try:
                        from tools import rpc_tools  # pylint: disable=C0415,E0401
                        result = rpc_tools.RpcMixin().rpc.timeout(5).elitea_core_usage_entity_meta(
                            project_id, conversation_name_search=search, conversation_search_limit=1000,
                        ) or {}
                        matching_uuids = [
                            c_item["uuid"]
                            for c_item in result.get("conversations") or []
                            if c_item.get("uuid")
                        ]
                    except Exception:  # pylint: disable=W0703
                        matching_uuids = []

                    if not matching_uuids:
                        return {"total": 0, "rows": []}, 200

                    conditions.append(UsageEvent.conversation_id.in_(matching_uuids))

                total, rows = self._chats(conditions, sort_by, sort_order, limit, offset)

                enriched = ac.resolve_chat_meta(project_id, rows, conditions=conditions)

                # Resolve owner_id → owner_email for the page
                owner_ids = [r.get("owner_id") for r in enriched if r.get("owner_id")]
                emails = an.label_users(owner_ids, {}) if owner_ids else {}
                for row in enriched:
                    row["owner_email"] = emails.get(row.get("owner_id"))

                return {"total": total, "rows": enriched}, 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics chats query failed", exc_info=True)
                return {"error": "Failed to query analytics chats"}, 500

        @staticmethod
        def _chats(conditions, sort_by, sort_order, limit, offset):
            """Grouped-by-conversation rows, paginated and sorted."""
            responses_col = func.count(distinct(UsageEvent.run_id)).label("responses")
            users_col = func.count(distinct(UsageEvent.user_id)).label("users")
            errors_col = ac.chat_error_responses_expr().label("errors")
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
            participant_types_col = func.array_agg(
                distinct(UsageEvent.root_entity_type)
            ).label("participant_types")

            cost_split_cols = {
                key: func.sum(func.coalesce(column, 0)).label(f"{key}_nano")
                for key, column in an.COST_SPLIT_COLUMNS.items()
            }

            columns = [
                UsageEvent.conversation_id.label("conversation_uuid"),
                responses_col,
                users_col,
                errors_col,
                last_activity_col,
                avg_dur_col,
                input_tokens_col,
                output_tokens_col,
                cache_read_tokens_col,
                cache_creation_tokens_col,
                total_tokens_col,
                cost_nano_col,
                participant_types_col,
                *cost_split_cols.values(),
            ]

            stmt = select(*columns).where(*conditions).group_by(UsageEvent.conversation_id)

            total_row = an.fetch_one(
                select(func.count().label("total")).select_from(stmt.subquery())
            )
            total = (total_row["total"] if total_row else 0) or 0

            sort_map = {
                "responses": responses_col,
                "users": users_col,
                "errors": errors_col,
                "last_activity": last_activity_col,
                "avg_duration_ms": avg_dur_col,
                "total_tokens": total_tokens_col,
                "llm_cost": cost_nano_col,
                "input_tokens": input_tokens_col,
                "output_tokens": output_tokens_col,
                "cache_read_tokens": cache_read_tokens_col,
                "cache_creation_tokens": cache_creation_tokens_col,
                **cost_split_cols,
                # chat_name and avg_cost_per_response are computed in Python, default to last_activity
                "chat_name": last_activity_col,
                "avg_cost_per_response": cost_nano_col,
            }

            order_fn = desc if sort_order == "desc" else asc
            rows = an.fetch_all(
                stmt.order_by(
                    order_fn(sort_map.get(sort_by, last_activity_col)),
                    UsageEvent.conversation_id,
                ).limit(limit).offset(offset)
            )

            _TYPE_MAP = {None: "model", "application": "agent", "pipeline": "pipeline", "skill": "skill"}
            result = []
            for r in rows:
                responses = int(r["responses"] or 0)
                cost_nano = r["cost_nano"] or 0
                # Keep None (= direct model call), deduplicate, preserve known order
                raw_types = r["participant_types"] or []
                seen = set()
                participants = []
                for raw in raw_types:
                    t = _TYPE_MAP.get(raw, "model")
                    if t not in seen:
                        seen.add(t)
                        participants.append({"type": t})
                result.append({
                    "conversation_uuid": r["conversation_uuid"],
                    "responses": responses,
                    "users": r["users"] or 0,
                    "errors": int(r["errors"] or 0),
                    "last_activity": r["last_activity"].isoformat() if r["last_activity"] else None,
                    "avg_duration_ms": an.rounded(r["avg_duration_ms"], 1),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "llm_cost": an.cost_usd(cost_nano),
                    **{key: an.cost_usd(r[f"{key}_nano"]) for key in an.COST_SPLIT_COLUMNS},
                    "avg_cost_per_response": (
                        round(an.cost_usd(cost_nano) / responses, 9) if responses else 0
                    ),
                    "participant_types": participants,
                })
            return total, result


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

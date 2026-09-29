"""
Single-run analytics detail endpoint: the full LLM/tool/sub-agent trace for one run_id.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request
    from sqlalchemy import select

    from ...methods import _analytics as an
    from ...models.usage_event import UsageEvent

    class PromptLibAPI(api_tools.APIModeHandler):
        """Full LLM/tool/sub-agent breakdown for one run."""

        @register_openapi(
            name="Get Run Analytics Detail",
            description=(
                "Returns the full event-level trace for one run_id: the LLM calls, tool calls, "
                "and sub-agents/sub-pipelines that happened during that run."
            ),
            mcp_tool=True,
            mcp_description="Use this tool when you need the full trace for one specific run: its LLM calls, tool calls, and sub-agents/sub-pipelines. Do not use this tool for aggregate breakdowns across many runs — use Get Agent Analytics Detail instead.",
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
                *an.RUN_SCOPE_PARAMETERS,
            ],
            responses={
                "200": {"description": "Run analytics detail"},
                "400": {"description": "run_id is required"},
                "401": {"description": "Unauthorized"},
                "404": {"description": "Run not found"},
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
            GET /api/v2/usage/analytics_run_detail/prompt_lib/<project_id>
            """
            try:
                run_scope, error = an.request_run_scope(project_id, request.args)
                if error:
                    return error
                if run_scope is None:
                    return {"error": "run_id is required"}, 400

                conditions = [UsageEvent.project_id == project_id, *an.run_filters(run_scope)]
                if run_scope.dt_from:
                    conditions.append(UsageEvent.ts >= run_scope.dt_from)
                if run_scope.dt_to:
                    conditions.append(UsageEvent.ts <= run_scope.dt_to)
                run_id = run_scope.run_id

                rows = an.fetch_all(select(
                    UsageEvent.entity_type,
                    UsageEvent.entity_id,
                    UsageEvent.entity_name,
                    UsageEvent.root_entity_type,
                    UsageEvent.root_entity_id,
                    UsageEvent.user_id,
                    UsageEvent.event_type,
                    UsageEvent.model_name,
                    UsageEvent.tool_name,
                    UsageEvent.input_tokens,
                    UsageEvent.output_tokens,
                    UsageEvent.cache_read_tokens,
                    UsageEvent.cache_creation_tokens,
                    UsageEvent.cost_nano_usd,
                    UsageEvent.duration_ms,
                    UsageEvent.is_error,
                    UsageEvent.ts,
                ).where(*conditions).order_by(UsageEvent.ts.asc()))

                if not rows:
                    return {"error": "Run not found"}, 404

                return self._build_response(run_id, rows), 200
            except Exception:  # pylint: disable=W0703
                log.error("Analytics run detail query failed", exc_info=True)
                return {"error": "Failed to query analytics run detail"}, 500

        @staticmethod
        def _build_response(run_id, rows):
            first_row = rows[0]
            root_entity_type = first_row["root_entity_type"]
            root_entity_id = first_row["root_entity_id"]

            root_entity_name = None
            for row in rows:
                if row["entity_type"] == root_entity_type and row["entity_id"] == root_entity_id:
                    root_entity_name = row["entity_name"]
                    break

            started_at = min(row["ts"] for row in rows)
            ended_at = max(row["ts"] for row in rows)

            llm_calls = []
            tool_calls = []
            sub_agents = {}
            total_tokens = 0
            total_cost_nano_usd = 0
            errors = 0

            for row in rows:
                total_tokens += int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0)
                total_cost_nano_usd += int(row["cost_nano_usd"] or 0)
                if row["is_error"]:
                    errors += 1

                if row["event_type"] == an.EVENT_LLM:
                    llm_calls.append({
                        "entity_type": row["entity_type"],
                        "entity_id": row["entity_id"],
                        "entity_name": row["entity_name"],
                        "model_name": row["model_name"],
                        "input_tokens": int(row["input_tokens"] or 0),
                        "output_tokens": int(row["output_tokens"] or 0),
                        "cache_read_tokens": int(row["cache_read_tokens"] or 0),
                        "cache_creation_tokens": int(row["cache_creation_tokens"] or 0),
                        "cost_nano_usd": int(row["cost_nano_usd"] or 0),
                        "duration_ms": row["duration_ms"],
                        "ts": row["ts"].isoformat() if row["ts"] else None,
                        "is_error": bool(row["is_error"]),
                    })
                elif row["event_type"] == an.EVENT_TOOL:
                    tool_calls.append({
                        "entity_type": row["entity_type"],
                        "entity_id": row["entity_id"],
                        "entity_name": row["entity_name"],
                        "tool_name": row["tool_name"],
                        "duration_ms": row["duration_ms"],
                        "ts": row["ts"].isoformat() if row["ts"] else None,
                        "is_error": bool(row["is_error"]),
                    })

                is_agent = (
                    row["root_entity_type"] in an.AGENT_ROOT_TYPES
                    and (row["entity_type"] or "") != an.ENTITY_TYPE_EVALUATION
                )
                if is_agent and row["entity_id"] != root_entity_id:
                    key = (row["entity_type"], row["entity_id"])
                    entry = sub_agents.setdefault(key, {
                        "entity_type": row["entity_type"],
                        "entity_id": row["entity_id"],
                        "entity_name": row["entity_name"],
                        "event_count": 0,
                    })
                    entry["event_count"] += 1

            return {
                "run_id": run_id,
                "root_entity_type": root_entity_type,
                "root_entity_id": root_entity_id,
                "root_entity_name": root_entity_name,
                "user_id": first_row["user_id"],
                "started_at": started_at.isoformat() if started_at else None,
                "ended_at": ended_at.isoformat() if ended_at else None,
                "duration_ms": (
                    int((ended_at - started_at).total_seconds() * 1000)
                    if started_at and ended_at else 0
                ),
                "kpis": {
                    "llm_calls": len(llm_calls),
                    "tool_calls": len(tool_calls),
                    "total_tokens": total_tokens,
                    "total_cost_nano_usd": total_cost_nano_usd,
                    "errors": errors,
                },
                "llm_calls": llm_calls,
                "tool_calls": tool_calls,
                "sub_agents": list(sub_agents.values()),
            }


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None

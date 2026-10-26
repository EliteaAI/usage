"""Shared helpers for the chat analytics endpoints."""

import time

from sqlalchemy import case, distinct, func, select

from ._analytics import (
    COST_SPLIT_COLUMNS,
    EVENT_LLM,
    METERED_EVENT_TYPES,
    base_filters,
    clamp_int,
    cost_usd,
    fetch_all,
    fetch_one,
    model_labels,
    model_scope_expr,
    rounded,
    total_tokens_expr,
)
from ._skill_analytics import label_model
from ..models.usage_event import UsageEvent

# Conversation kinds that are NOT user chats and must be excluded.
# Resolved at query time via RPC, bounded and cached.
_EXCLUDED_UUID_CACHE = {}  # {project_id: (monotonic_stamp, frozenset_of_uuids)}
_EXCLUDED_UUID_TTL = 60  # seconds


def _get_excluded_uuids(project_id):
    """Return the set of conversation UUIDs that are NOT user chats (hidden/non-elitea).

    Calls elitea_core via RPC with a bounded query (<=5000 rows) and caches for 60 s.
    On RPC failure, returns an empty set (chats with unknown kind are included, not dropped).
    """
    now = time.monotonic()
    cached = _EXCLUDED_UUID_CACHE.get(project_id)
    if cached and (now - cached[0]) < _EXCLUDED_UUID_TTL:
        return cached[1]

    try:
        from tools import rpc_tools  # pylint: disable=C0415,E0401
        result = rpc_tools.RpcMixin().rpc.timeout(5).elitea_core_usage_entity_meta(
            project_id, fetch_non_chat_uuids=True,
        ) or {}
        uuids = frozenset(result.get("non_chat_uuids") or [])
    except Exception:  # pylint: disable=W0703
        uuids = frozenset()

    _EXCLUDED_UUID_CACHE[project_id] = (now, uuids)
    return uuids


def chat_base_filters(project_id, dt_from, dt_to):
    """base_filters + conversation_id IS NOT NULL + exclude non-chat conversations."""
    conditions = base_filters(project_id, dt_from, dt_to)
    conditions.append(UsageEvent.conversation_id.isnot(None))

    excluded = _get_excluded_uuids(project_id)
    if excluded:
        conditions.append(UsageEvent.conversation_id.notin_(list(excluded)))

    return conditions


def chat_responses_expr():
    """Distinct run_ids per conversation = number of AI responses."""
    return func.count(distinct(UsageEvent.run_id))


def chat_error_responses_expr():
    """Distinct run_ids that had at least one is_error=True call."""
    return func.count(distinct(case(
        (UsageEvent.is_error.is_(True), UsageEvent.run_id),
        else_=None,
    )))


def _fetch_models_by_conversation(project_id, conditions, conversation_uuids):
    """Return {conversation_uuid: [{model_name, display_name}, ...]} for the given UUIDs."""
    if not conversation_uuids:
        return {}
    scope_expr = model_scope_expr()
    rows = fetch_all(
        select(
            UsageEvent.conversation_id,
            UsageEvent.model_name,
            scope_expr.label("model_scope"),
        )
        .where(
            *conditions,
            UsageEvent.event_type == EVENT_LLM,
            UsageEvent.conversation_id.in_(conversation_uuids),
            UsageEvent.model_name.isnot(None),
        )
        .group_by(UsageEvent.conversation_id, UsageEvent.model_name, scope_expr)
    )
    if not rows:
        return {}
    labels = model_labels(
        project_id,
        [{"model_name": r["model_name"], "model_scope": r["model_scope"]} for r in rows],
    )
    result = {}
    for r in rows:
        result.setdefault(r["conversation_id"], []).append({
            "model_name": r["model_name"],
            "display_name": label_model(r["model_name"], labels, r["model_scope"]),
        })
    for conv_models in result.values():
        conv_models.sort(key=lambda m: (m["display_name"] is None, m["display_name"] or ""))
    return result


def resolve_chat_meta(project_id, rows, conditions=None):
    """Enrich a page of rows with name, is_private, is_deleted, can_open, owner, participants, models.

    Calls elitea_core_usage_entity_meta for the page's conversation UUIDs only.
    Missing UUIDs (deleted conversations) come back as is_deleted=True.
    Pass conditions (from chat_base_filters) to also resolve per-conversation model lists.
    """
    uuids = [r["conversation_uuid"] for r in rows if r.get("conversation_uuid")]
    if not uuids:
        return rows

    try:
        from tools import rpc_tools  # pylint: disable=C0415,E0401
        result = rpc_tools.RpcMixin().rpc.timeout(5).elitea_core_usage_entity_meta(
            project_id, conversation_uuids=uuids,
        ) or {}
        meta_map = {c["uuid"]: c for c in result.get("conversations") or []}
    except Exception:  # pylint: disable=W0703
        meta_map = {}

    models_by_conv = _fetch_models_by_conversation(project_id, conditions or [], uuids) if conditions else {}

    enriched = []
    for row in rows:
        uuid = row.get("conversation_uuid")
        meta = meta_map.get(uuid, {})
        is_deleted = not bool(meta)
        # Prefer participant types resolved from chat_participants (accurate pipeline/agent split).
        # Fall back to usage-event-derived types for conversations that predate the RPC enrichment.
        conv_participant_types = meta.get("participant_types")
        if conv_participant_types is not None:
            participants = [{"type": t} for t in conv_participant_types]
        else:
            participants = row.get("participant_types") or []
        enriched.append({
            **row,
            "chat_name": meta.get("name") or (
                "Private chat" if meta.get("is_private") else f"Chat {uuid[:8]}"
            ),
            "is_private": meta.get("is_private", True),
            "is_deleted": is_deleted,
            "can_open": not is_deleted and not meta.get("is_private", True),
            "conversation_id": meta.get("id"),
            "owner_id": meta.get("author_id"),
            "participants": participants,
            "models": models_by_conv.get(uuid, []),
        })
    return enriched



"""Shared helpers for the chat analytics endpoints."""

import time

from pylon.core.tools import log  # pylint: disable=E0611,E0401
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
_EXCLUDED_UUID_CACHE_MAX = 64  # projects


def _get_excluded_uuids(project_id):
    """Return the set of conversation UUIDs that are NOT user chats (hidden, or not a chat source).

    Calls elitea_core via RPC with a bounded query (<=5000 rows) and caches a successful answer
    for 60 s. When the RPC fails the answer is empty and is NOT cached, so the next request asks
    again instead of showing every non-chat conversation for the whole TTL.
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
    except Exception:  # pylint: disable=W0703
        log.warning("usage: non-chat conversations unavailable for project %s", project_id)
        return frozenset()

    uuids = frozenset(result.get("non_chat_uuids") or [])
    if result.get("non_chat_truncated"):
        log.warning(
            "usage: non-chat conversation list truncated for project %s, some may show as chats",
            project_id,
        )

    if len(_EXCLUDED_UUID_CACHE) >= _EXCLUDED_UUID_CACHE_MAX:
        for key, entry in list(_EXCLUDED_UUID_CACHE.items()):
            if (now - entry[0]) >= _EXCLUDED_UUID_TTL:
                _EXCLUDED_UUID_CACHE.pop(key, None)
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


PRIVATE_CHAT_NAME = "Private chat"


def resolve_chat_meta(project_id, rows, conditions=None, requester_id=None):
    """Enrich a page of rows with name, is_private, is_deleted, can_open, owner, participants, models.

    Calls elitea_core_usage_entity_meta for the page's conversation UUIDs only.
    UUIDs core does not know come back as is_deleted=True. When core cannot be reached nothing is
    known, so no row is reported deleted: meta_unavailable is set and the name is a placeholder.
    A private chat's name is shown only to its author (requester_id).
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
        log.warning("usage: chat names unavailable for project %s", project_id)
        meta_map = None

    models_by_conv = _fetch_models_by_conversation(project_id, conditions or [], uuids) if conditions else {}

    enriched = []
    for row in rows:
        uuid = row.get("conversation_uuid")
        meta_unavailable = meta_map is None
        meta = {} if meta_unavailable else meta_map.get(uuid, {})
        is_deleted = not meta_unavailable and not bool(meta)
        is_private = meta.get("is_private", True)
        is_author = requester_id is not None and meta.get("author_id") == requester_id
        if meta_unavailable or is_deleted:
            chat_name = f"Chat {uuid[:8]}"
        elif is_private and not is_author:
            chat_name = PRIVATE_CHAT_NAME
        else:
            chat_name = meta.get("name") or (PRIVATE_CHAT_NAME if is_private else f"Chat {uuid[:8]}")
        # Prefer participant types resolved from chat_participants (accurate pipeline/agent split).
        # Fall back to usage-event-derived types for conversations that predate the RPC enrichment.
        conv_participant_types = meta.get("participant_types")
        if conv_participant_types is not None:
            participants = [{"type": t} for t in conv_participant_types]
        else:
            participants = row.get("participant_types") or []
        enriched.append({
            **row,
            "chat_name": chat_name,
            "is_private": is_private,
            "is_deleted": is_deleted,
            "meta_unavailable": meta_unavailable,
            "can_open": not is_deleted and not meta_unavailable and not is_private,
            "conversation_id": meta.get("id"),
            "owner_id": meta.get("author_id"),
            "participants": participants,
            "models": models_by_conv.get(uuid, []),
        })
    return enriched



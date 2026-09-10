#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Interface hooks — the two call sites every runtime interface contributes

Resolved lazily at call time (tools.usage_hooks), so neither side needs init_after.
"""

import dataclasses
import datetime
import time
import typing
import uuid

from pylon.core.tools import log  # pylint: disable=E0611,E0401

from tools import context, this  # pylint: disable=E0401

from .methods.mode import MODE_OFF, normalize_mode
from .sources import base, registry

EVENT_TYPE_LLM = "llm"
COST_SOURCE_UNPRICED = "unpriced"
RUN_ID_HEADER = "X-Elitea-Run-Id"

# Enough for any provider's first frame; the dialects are incremental, so nothing else is kept
HEAD_LIMIT = 8192


@dataclasses.dataclass
class UsageContext:  # pylint: disable=R0902
    """Handle an interface carries from begin_llm_call to meter_llm_response."""

    project_id: int = None
    user_id: int = None
    model_name: str = None
    endpoint: str = None
    denied: bool = False
    response: typing.Any = None
    provider: str = None
    run_id: str = None
    user_email: str = None
    idempotency_key: str = None
    start_time_ns: int = None


def begin_llm_call(  # pylint: disable=R0913,R0917
        project_id, user_id, model_name, endpoint, headers, provider=None,
):
    """None when metering is inactive; a UsageContext otherwise.

    model_name is RAW/pre-mapping: LiteLLM rewrites it, the costs catalog uses the raw name.
    `provider` is a keyword so an interface that cannot resolve it still fits the contract.
    """
    if _mode() == MODE_OFF:
        return None
    #
    return UsageContext(
        project_id=project_id if project_id else _resolve_project_id(user_id, headers),
        user_id=user_id,
        model_name=model_name,
        endpoint=endpoint,
        provider=provider,
        run_id=(headers or {}).get(RUN_ID_HEADER),
        idempotency_key=uuid.uuid4().hex,
        start_time_ns=time.monotonic_ns(),
    )


def meter_llm_response(ctx, response, iterator):
    """Returns the iterator to serve. Identity while there is nothing to meter."""
    if ctx is None:
        return iterator
    #
    return _metered(ctx, response, iterator)


def _metered(ctx, response, iterator):
    """Chunks pass straight through; the row is written once the body is done or abandoned."""
    status = _status_of(response)
    dialect = None
    probed = False
    #
    try:
        for chunk in iterator:
            # An error body is relayed untouched: there is nothing to read and the client needs it
            if status >= 400:
                yield chunk
                continue
            #
            if not probed:
                probed = True
                dialect = registry.match(
                    ctx.endpoint, _content_type_of(response),
                    _head_of(chunk), provider=ctx.provider,
                )
            #
            if dialect is not None:
                _feed(dialect, chunk)
            #
            yield chunk
    #
    finally:
        # In finally so a client disconnect mid-stream is still billed, and after the last
        # byte so the insert never sits in the user-visible latency path
        _record(ctx, dialect, status)


def _feed(dialect, chunk):
    """A broken dialect degrades the row to unparsed; it never breaks the response."""
    try:
        dialect.feed(chunk)
    except:  # pylint: disable=W0702
        log.exception("usage: dialect %s failed to read a chunk", getattr(dialect, "id", None))


def _record(ctx, dialect, status):
    """One row per call — provider tokens, an upstream error, or an explicit unparsed."""
    try:
        if ctx.project_id is None:
            log.warning("usage: no project resolved for user %s; call not recorded", ctx.user_id)
            return
        #
        this.module.usage_write_event(_row(ctx, _reading_of(dialect), status))
    except:  # pylint: disable=W0702
        log.exception("usage: failed to record a metered call")


def _reading_of(dialect):
    """Whatever the dialect saw, or an empty unparsed reading when there was no dialect."""
    if dialect is None:
        return base.UsageReading(token_source=base.TOKEN_SOURCE_UNPARSED)
    #
    try:
        reading = dialect.result()
    except:  # pylint: disable=W0702
        log.exception("usage: dialect %s failed to report", getattr(dialect, "id", None))
        return base.UsageReading(
            token_source=base.TOKEN_SOURCE_UNPARSED, dialect=getattr(dialect, "id", None),
        )
    #
    if reading.input_tokens is None and reading.output_tokens is None:
        reading.token_source = base.TOKEN_SOURCE_UNPARSED
    #
    return reading


def _row(ctx, reading, status):  # pylint: disable=R0914
    """The usage_event payload. A call is always recorded, never dropped for being unreadable."""
    billable = base.billable_input_tokens(reading)
    cost, cost_source = _price(
        ctx.model_name, billable, reading.output_tokens,
        reading.cache_read_tokens, reading.cache_creation_tokens,
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    #
    return {
        "ts": now,
        "idempotency_key": ctx.idempotency_key,
        "project_id": ctx.project_id,
        "user_id": ctx.user_id,
        "user_email": ctx.user_email,
        "run_id": ctx.run_id,
        "event_type": EVENT_TYPE_LLM,
        "model_name": ctx.model_name,
        "dialect": reading.dialect,
        "endpoint": ctx.endpoint,
        "input_tokens": reading.input_tokens or 0,
        "output_tokens": reading.output_tokens or 0,
        "cache_read_tokens": reading.cache_read_tokens or 0,
        "cache_creation_tokens": reading.cache_creation_tokens or 0,
        "reasoning_tokens": reading.reasoning_tokens or 0,
        "billable_input_tokens": billable or 0,
        # 0 only ever alongside cost_source='unpriced', so a zero is never mistaken for free
        "cost_micro_usd": 0 if cost is None else int(round(cost * 1_000_000)),
        "cost_source": cost_source,
        "token_source": reading.token_source,
        "duration_ms": _elapsed_ms(ctx),
        "is_error": status >= 400,
    }


def _price(model_name, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens):
    """(cost_usd, cost_source); an unpriced model is marked, never silently zero."""
    try:
        priced = context.rpc_manager.timeout(10).costs_compute_llm_cost(
            model_name=model_name,
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            cache_read_input_tokens=cache_read_tokens or 0,
            cache_creation_input_tokens=cache_creation_tokens or 0,
        ) or {}
    except:  # pylint: disable=W0702
        log.exception("usage: pricing lookup failed for %s", model_name)
        priced = {}
    #
    cost = priced.get("cost")
    #
    if cost is None:
        return None, COST_SOURCE_UNPRICED
    #
    return cost, priced.get("cost_source") or COST_SOURCE_UNPRICED


def _elapsed_ms(ctx):
    if ctx.start_time_ns is None:
        return None
    #
    return int((time.monotonic_ns() - ctx.start_time_ns) / 1_000_000)


def _head_of(chunk):
    """First-frame probe window; a non-bytes chunk cannot be sniffed."""
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk[:HEAD_LIMIT])
    #
    return b""


def _status_of(response):
    try:
        return int((response or {}).get("status_code") or 200)
    except (AttributeError, TypeError, ValueError):
        return 200


def _content_type_of(response):
    for key, value in _headers_of(response):
        if str(key).lower() == "content-type":
            return value
    #
    return None


def _headers_of(response):
    headers = (response or {}).get("headers") if hasattr(response, "get") else None
    #
    if headers is None:
        return []
    #
    if hasattr(headers, "items"):
        return list(headers.items())
    #
    return [pair for pair in headers if isinstance(pair, (list, tuple)) and len(pair) == 2]


def _mode():
    try:
        return normalize_mode((this.descriptor.config.get("usage") or {}).get("mode"))
    except:  # pylint: disable=W0702
        log.exception("usage: cannot read the usage mode; metering stays off")
        return MODE_OFF


def _resolve_project_id(user_id, headers):
    try:
        return this.module.usage_resolve_project_id(user_id, None, headers)
    except:  # pylint: disable=W0702
        log.exception("usage: failed to resolve the project for user %s", user_id)
        return None

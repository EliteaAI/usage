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

""" What a runtime interface calls: two functions over the proxy dicts it already has

Every decision that can be made from those dicts is made here, so an interface carries no
metering policy at all — it hands over the two facts only it knows (the pre-mapping model name
and the project the model resolved in) and is done. Both functions are total: they never raise,
and whether to record is read from this plugin's own mode, never from anything a caller sends.
"""

import cachetools  # pylint: disable=E0401

from pylon.core.tools import log  # pylint: disable=E0611,E0401

from tools import context, this  # pylint: disable=E0401

from . import hooks
from .methods.estimate import output_tokens_of
from .methods.mode import MODE_OFF
from .sources.dialect import path_of

# Set on proxy_auth by prepare_llm_call and read back by meter_llm_call. Owned here so an
# interface never names them; presence of the model key is also what marks a call as prepared.
PROVIDER_AUTH_KEY = "usage_provider"
RAW_MODEL_AUTH_KEY = "usage_raw_model"

# Already canonicalised by the interface that parked it (#6569); re-validated in hooks anyway
RUN_ID_AUTH_KEY = "platform_run_id"

# The context is built before the call, not after: only here can it still be refused
CONTEXT_AUTH_KEY = "usage_context"

# Raw X-Elitea-Attribution value, parked by the interface that stripped it; decoded in hooks
ATTRIBUTION_AUTH_KEY = "platform_attribution"

# Chat/legacy completions alone gate streamed usage behind include_usage. Responses, messages,
# converse and the rest always report it, and do not accept the field — sending it risks a 400.
STREAM_USAGE_PATHS = ("/chat/completions", "/completions")

# Credentials change rarely and this must not become a DB query per LLM call
_provider_cache = cachetools.TTLCache(maxsize=4096, ttl=60)


def prepare_llm_call(proxy_target, proxy_auth, raw_model_name=None, model_project_id=None):
    """None to proceed, or the response to return instead of calling the model. Never raises."""
    try:
        if hooks.current_mode() == MODE_OFF:
            return None
        #
        proxy_auth[RAW_MODEL_AUTH_KEY] = raw_model_name
        proxy_auth[PROVIDER_AUTH_KEY] = resolve_provider(model_project_id, raw_model_name)
        #
        request_usage_frame(proxy_target)
        #
        usage_context = hooks.begin_llm_call(
            project_id=proxy_auth.get("project_id"),
            user_id=(proxy_auth.get("user") or {}).get("id"),
            model_name=raw_model_name,
            endpoint=proxy_target.get("endpoint"),
            headers=proxy_target.get("headers"),
            provider=proxy_auth.get(PROVIDER_AUTH_KEY),
            run_id=proxy_auth.get(RUN_ID_AUTH_KEY),
            attribution=proxy_auth.get(ATTRIBUTION_AUTH_KEY),
            max_output_tokens=requested_output_tokens(proxy_target),
            input_size_bytes=request_size_of(proxy_target),
        )
        #
        proxy_auth[CONTEXT_AUTH_KEY] = usage_context
        #
        if usage_context is not None and usage_context.denied:
            return usage_context.response
        #
        return None
    except:  # pylint: disable=W0702
        log.exception("usage: failed to prepare an LLM call")
        return None


def requested_output_tokens(proxy_target):
    """The output ceiling this request asked for, else the configured default."""
    return output_tokens_of(
        proxy_target.get("json"),
        this.module.usage_default_output_tokens(),
    )


def request_size_of(proxy_target):
    """Content-Length only — re-serializing a body to measure it is a known outage class."""
    headers = proxy_target.get("headers") or {}
    #
    try:
        return int(headers.get("Content-Length") or headers.get("content-length") or 0)
    except (TypeError, ValueError):
        return 0


def meter_llm_call(proxy_target, proxy_auth, response, iterator):
    """The iterator to serve. Identity unless prepare_llm_call marked this call billable."""
    if RAW_MODEL_AUTH_KEY not in proxy_auth:
        return iterator
    #
    try:
        return hooks.meter_llm_response(proxy_auth.get(CONTEXT_AUTH_KEY), response, iterator)
    except:  # pylint: disable=W0702
        # A metering failure must never cost the user their response
        log.exception("usage: failed to meter an LLM call")
        return iterator


def request_usage_frame(proxy_target):
    """Streamed OpenAI-family calls report no usage unless include_usage is asked for."""
    body = proxy_target.get("json")
    #
    if not isinstance(body, dict) or not body.get("stream"):
        return
    #
    if not path_of(proxy_target.get("endpoint")).rstrip("/").endswith(STREAM_USAGE_PATHS):
        return
    #
    options = body.get("stream_options")
    options = dict(options) if isinstance(options, dict) else {}
    # Forced, not defaulted: a caller sending include_usage=false would otherwise go unbilled
    options["include_usage"] = True
    body["stream_options"] = options


def resolve_provider(project_id, raw_model_name):
    """Credential family behind a model, cached because the lookup queries Postgres.

    Scoped to the project the model actually resolved in, so a private model cannot label a
    same-named public one. A failure degrades metering to body sniffing; it never fails the call.
    """
    if project_id is None or not raw_model_name:
        return None
    #
    key = (project_id, raw_model_name)
    #
    if key in _provider_cache:
        return _provider_cache[key]
    #
    try:
        provider = context.rpc_manager.timeout(10).configurations_get_model_provider(
            project_id=project_id, model_name=raw_model_name,
        )
    except:  # pylint: disable=W0702
        log.exception("usage: failed to resolve the provider for model %s", raw_model_name)
        return None
    #
    _provider_cache[key] = provider
    return provider

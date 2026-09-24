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

""" What a call is reserved for before it runs

A per-call estimate, not a flat ceiling: a flat max-cost reservation times the concurrency
would refuse a project that still has real budget left.
"""

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import context  # pylint: disable=E0401

from ._counters import NANO

DEFAULT_OUTPUT_TOKENS = 4096
DEFAULT_MAX_CALL_COST_USD = 2.0

OUTPUT_LIMIT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")

# Rough bytes-per-token for the request body; the estimate only has to be the right size
BYTES_PER_TOKEN = 4


def output_tokens_of(body, default_output_tokens=DEFAULT_OUTPUT_TOKENS):
    """The output ceiling the caller asked for, else the configured default."""
    if hasattr(body, "get"):
        for field in OUTPUT_LIMIT_FIELDS:
            value = body.get(field)
            #
            try:
                if value is not None and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                continue
    #
    return int(default_output_tokens)


def input_tokens_of(input_size_bytes):
    """Input size from Content-Length. The body is never re-serialized to measure it."""
    try:
        return max(0, int(input_size_bytes or 0)) // BYTES_PER_TOKEN
    except (TypeError, ValueError):
        return 0


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_estimate_nano(self, model_name, max_output_tokens, input_size_bytes):
        """Nano-USD to reserve for one call. 0 when it cannot be priced, so it never refuses."""
        if not model_name:
            return 0
        #
        reservation = self.usage_config().get("reservation") or {}
        #
        try:
            priced = context.rpc_manager.timeout(10).costs_compute_llm_cost(
                model_name=model_name,
                input_tokens=input_tokens_of(input_size_bytes),
                output_tokens=int(max_output_tokens or 0),
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ) or {}
        except:  # pylint: disable=W0702
            log.exception("usage: cannot price a reservation for %s", model_name)
            return 0
        #
        cost = priced.get("cost")
        #
        if cost is None:
            return 0
        #
        ceiling = float(reservation.get("max_call_cost_usd", DEFAULT_MAX_CALL_COST_USD))
        #
        return int(round(min(float(cost), ceiling) * NANO))

    @web.method()
    def usage_default_output_tokens(self):
        """Output ceiling assumed when the caller names none."""
        reservation = self.usage_config().get("reservation") or {}
        #
        return int(reservation.get("default_output_tokens", DEFAULT_OUTPUT_TOKENS))

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

""" Usage plugin — usage_event fact table, usage_counter ledger, mode gating

All aggregation SQL over both tables lives here as named RPCs. Not a DSL — see rpc/facade.py.
"""

from queue import Empty

from pylon.core.tools import log, module  # pylint: disable=E0611,E0401

from .hooks import begin_llm_call, meter_llm_response
from .interface import meter_llm_call, prepare_llm_call, request_usage_frame
from .methods.mode import MODE_ENFORCE, MODE_OFF
from .sources import registry


class Module(module.ModuleModel):
    """ Pylon module """

    def __init__(self, context, descriptor):
        self.context = context
        self.descriptor = descriptor

    def init(self):
        """ Initialize module """
        log.info("Initializing usage plugin")
        self.descriptor.init_all()
        #
        # Registers both tables in the shared metadata; provisioning is shared's job
        from .models import usage_counter, usage_event  # pylint: disable=C0415,W0611
        #
        registry.register_defaults()
        #
        self.descriptor.register_tool("usage_hooks", self)

    def ready(self):
        """ Ready callback """
        # After shared.ready() created the parent table — usage depends_on shared
        self.usage_ensure_partitions()
        self.usage_report_interfaces()
        self._warn_if_metering_expected()
        self._register_cron()

    def reconfig(self):
        """ Re-config """
        log.info(
            "usage reconfigured: mode=%s spend_source=%s",
            self.usage_get_mode(), self.usage_get_spend_source(),
        )
        self._warn_if_metering_expected()

    def deinit(self):
        """ De-initialize module """
        log.info("De-initializing usage plugin")

    # What a runtime interface calls; the three below are for an interface that knows its own
    # provider and so drives the lower level directly (WAM), instead of having it resolved
    prepare_llm_call = staticmethod(prepare_llm_call)
    meter_llm_call = staticmethod(meter_llm_call)
    begin_llm_call = staticmethod(begin_llm_call)
    meter_llm_response = staticmethod(meter_llm_response)
    request_usage_frame = staticmethod(request_usage_frame)

    def _warn_if_metering_expected(self):
        """Enforcement is not wired yet, so an operator asking for it must be told."""
        mode = self.usage_get_mode()
        #
        if mode == MODE_ENFORCE:
            log.warning(
                "usage: mode is enforce but admission control is not implemented yet; calls are "
                "metered and none are refused",
            )
        elif mode != MODE_OFF:
            log.info("usage: metering LLM calls, mode=%s", mode)

    def _register_cron(self):
        try:
            self.context.rpc_manager.timeout(5).scheduling_create_if_not_exists({
                "rpc_func": "usage_ensure_partitions",
                "rpc_kwargs": {},
                "name": "usage_ensure_partitions",
                "cron": "0 3 * * *",
                "active": True,
            })
        except Empty:
            log.warning("usage: no scheduling plugin found; partition cron not registered")
        except Exception as exc:  # pylint: disable=W0703
            log.warning("usage: failed to register partition cron: %s", exc)

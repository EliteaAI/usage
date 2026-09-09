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

""" Runtime interface registry and startup diagnostics """

import sys

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from .mode import MODE_ENFORCE, MODE_OBSERVE, MODE_OFF

REGISTRY_TOOL = "runtime_interfaces"

INTERFACE_NAME_PREFIX = "runtime_interface_"


def registry():
    """The shared list every runtime_interface_* plugin appends itself to.

    Read defensively: a pylon with no interface plugin is a valid deployment.
    """
    return list(getattr(sys.modules["tools"], REGISTRY_TOOL, None) or [])


def declares_hooks(descriptor):
    """Whether an interface's metadata says it calls the usage hooks."""
    metadata = getattr(descriptor, "metadata", None) or {}
    #
    return bool(metadata.get("usage_hooks", False))


def describe(interface):
    """One interface's diagnostic record."""
    descriptor = getattr(interface, "descriptor", None)
    metadata = getattr(descriptor, "metadata", None) or {}
    config = getattr(descriptor, "config", None) or {}
    #
    return {
        "name": getattr(descriptor, "name", None) or metadata.get("name", "unknown"),
        "url_prefix": config.get("url_prefix", None),
        "usage_hooks": declares_hooks(descriptor),
    }


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_list_interfaces(self):
        """Diagnostic records for every registered runtime interface."""
        return [describe(interface) for interface in registry()]

    @web.method()
    def usage_unregistered_interfaces(self):
        """Plugins named like an interface that never appeared in the registry."""
        registered = {record["name"] for record in self.usage_list_interfaces()}
        #
        try:
            descriptors = self.context.module_manager.descriptors
        except AttributeError:
            return []
        #
        return [
            name for name in descriptors
            if name.startswith(INTERFACE_NAME_PREFIX) and name not in registered
        ]

    @web.method()
    def usage_report_interfaces(self):
        """Log which interfaces will be metered, and which will not.

        In enforce an interface without usage_hooks is recorded refused; the refusal lands with the gate.
        """
        mode = self.usage_get_mode()
        records = self.usage_list_interfaces()
        refused = []
        #
        for record in records:
            if record["usage_hooks"]:
                log.info(
                    "usage: interface %s (%s) declares usage hooks",
                    record["name"], record["url_prefix"],
                )
                continue
            #
            if mode == MODE_ENFORCE:
                refused.append(record["name"])
                log.error(
                    "usage: interface %s does not declare usage hooks and is refused service "
                    "in enforce mode", record["name"],
                )
            elif mode == MODE_OBSERVE:
                log.warning(
                    "usage: interface %s does not declare usage hooks; its traffic is "
                    "unmetered", record["name"],
                )
            else:
                log.info(
                    "usage: interface %s does not declare usage hooks (mode is %s)",
                    record["name"], MODE_OFF,
                )
        #
        for name in self.usage_unregistered_interfaces():
            log.warning(
                "usage: plugin %s looks like a runtime interface but never registered itself",
                name,
            )
        #
        if not records:
            log.info("usage: no runtime interfaces registered")
        #
        return refused

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

""" Runtime interface enumeration and startup diagnostics """

import json

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from .mode import MODE_ENFORCE, MODE_OBSERVE, MODE_OFF

INTERFACE_NAME_PREFIX = "runtime_interface_"

UNMETERED_INTERFACE_MESSAGE = (
    "This runtime interface does not report usage, so it is disabled while usage is enforced."
)


def declares_hooks(descriptor):
    """Whether an interface's metadata says it calls the usage hooks."""
    metadata = getattr(descriptor, "metadata", None) or {}
    #
    return bool(metadata.get("usage_hooks", False))


def describe(name, descriptor):
    """One interface's diagnostic record."""
    config = getattr(descriptor, "config", None) or {}
    #
    return {
        "name": name,
        "url_prefix": config.get("url_prefix", None),
        "usage_hooks": declares_hooks(descriptor),
    }


def unmetered_response(name):
    """The refusal served instead of any request routed to an undeclared interface."""
    body = json.dumps({
        "error": {
            "message": UNMETERED_INTERFACE_MESSAGE,
            "type": "usage_unavailable",
            "code": "interface_unmetered",
            "interface": name,
        },
    }).encode("utf-8")
    #
    return body, 503, {"Content-Type": "application/json"}


def make_guard(name, get_mode):
    """A before_request hook refusing the interface's traffic; mode is read per request."""
    def guard():
        if get_mode() == MODE_ENFORCE:
            return unmetered_response(name)
        return None
    #
    guard.usage_guarded_interface = name
    return guard


def install_guard(app, name, get_mode):
    """Idempotently hook the blueprint; written to the dict since Flask forbids late setup."""
    funcs = app.before_request_funcs.setdefault(name, [])
    #
    if any(getattr(func, "usage_guarded_interface", None) == name for func in funcs):
        return False
    #
    funcs.insert(0, make_guard(name, get_mode))
    return True


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_list_interfaces(self):
        """Diagnostic records for every loaded runtime interface.

        Read from the loaded descriptors, so an interface needs to do nothing to be seen.
        """
        try:
            descriptors = self.context.module_manager.descriptors
        except AttributeError:
            return []
        #
        return [
            describe(name, descriptor) for name, descriptor in descriptors.items()
            if name.startswith(INTERFACE_NAME_PREFIX)
        ]

    @web.method()
    def usage_report_interfaces(self):
        """Log which interfaces are metered, and block every undeclared one while enforcing."""
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
                    "usage: interface %s does not declare usage hooks; in enforce mode its "
                    "traffic is refused with 503", record["name"],
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
        # Guarded in every mode: the mode is read per request, so a later switch to enforce blocks
        self.usage_guard_interfaces(
            [record["name"] for record in records if not record["usage_hooks"]],
        )
        #
        if not records:
            log.info("usage: no runtime interfaces loaded")
        #
        return refused

    @web.method()
    def usage_guard_interfaces(self, names):
        """Put a request guard on each named interface's blueprint; the names it could not guard."""
        app = getattr(self.context, "app", None)
        #
        if app is None or not hasattr(app, "before_request_funcs"):
            if names:
                log.critical("usage: no web app to guard unmetered interfaces: %s", ", ".join(names))
            return list(names)
        #
        for name in names:
            if install_guard(app, name, self.usage_get_mode):
                log.info("usage: request guard installed on unmetered interface %s", name)
        #
        return []

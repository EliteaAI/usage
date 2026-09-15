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

""" Usage mode gating """

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"

MODES = (MODE_OFF, MODE_OBSERVE, MODE_ENFORCE)

def normalize_mode(value):
    """Coerce a configured mode to one of MODES, falling back to off.

    Unquoted `off` is YAML boolean false, so False must read as off, not "False".
    """
    if value is None or value is False:
        return MODE_OFF
    #
    if value is True:
        log.warning("usage.mode is boolean true, which is ambiguous; treating as off")
        return MODE_OFF
    #
    mode = str(value).strip().lower()
    #
    if mode not in MODES:
        log.warning("Unknown usage.mode %r, treating as off", value)
        return MODE_OFF
    #
    return mode


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_config(self):
        """The plugin's own config block, re-read on every call so reconfig takes effect."""
        return self.descriptor.config.get("usage", None) or {}

    @web.method()
    def usage_get_mode(self):
        """Current metering mode: off, observe or enforce."""
        return normalize_mode(self.usage_config().get("mode", None))

    @web.method()
    def usage_is_enabled(self):
        """Whether anything is metered at all."""
        return self.usage_get_mode() != MODE_OFF

    @web.method()
    def usage_is_observing(self):
        """Metering without blocking."""
        return self.usage_get_mode() == MODE_OBSERVE

    @web.method()
    def usage_is_enforcing(self):
        """Metering with blocking."""
        return self.usage_get_mode() == MODE_ENFORCE

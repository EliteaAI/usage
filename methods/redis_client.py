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

""" Redis client for the counter ledger and the admission gate """

import redis  # pylint: disable=E0401

from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import config as c  # pylint: disable=E0401

DEFAULT_SOCKET_TIMEOUT = 2
DEFAULT_SOCKET_CONNECT_TIMEOUT = 2


class Method:  # pylint: disable=R0903
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_redis_client(self):
        """One long-lived client, built once. A fresh one per op churns connections on the hub."""
        client = getattr(self, "_usage_redis_client", None)  # pylint: disable=E1101
        #
        if client is not None:
            return client
        #
        redis_config = self.descriptor.config.get("redis_config", None)  # pylint: disable=E1101
        #
        if not redis_config:
            redis_config = {
                "host": c.REDIS_HOST,
                "port": c.REDIS_PORT,
                "db": c.REDIS_CHAT_CANVAS_DB,
                "username": c.REDIS_USER,
                "password": c.REDIS_PASSWORD,
                "ssl": c.REDIS_USE_SSL,
                "decode_responses": True,
            }
        #
        redis_config = redis_config.copy()
        #
        # Forced, not defaulted: the gate parses keys and lease ids out of replies, and bytes
        # would build literal b'...' key names instead of raising
        redis_config["decode_responses"] = True
        # This client is on the request path of every LLM call, so a half-open socket must not
        # park a greenlet without a deadline
        redis_config.setdefault("socket_timeout", DEFAULT_SOCKET_TIMEOUT)
        redis_config.setdefault("socket_connect_timeout", DEFAULT_SOCKET_CONNECT_TIMEOUT)
        #
        if redis_config.pop("use_managed_identity", False):
            redis_config.pop("password", None)
            #
            from redis_entraid.cred_provider import create_from_default_azure_credential  # pylint: disable=C0415,E0401
            #
            redis_config["credential_provider"] = create_from_default_azure_credential(
                ("https://redis.azure.com/.default",),
            )
        #
        client = redis.Redis(**redis_config)
        self._usage_redis_client = client  # pylint: disable=W0201
        #
        return client

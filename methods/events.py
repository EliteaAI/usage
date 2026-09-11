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

""" usage_event writer — one direct insert per metered call

Direct insert now; the Redis append plus batching drainer arrives with the counter ledger.
"""

from sqlalchemy.dialects.postgresql import insert

from pylon.core.tools import log  # pylint: disable=E0611,E0401
from pylon.core.tools import web  # pylint: disable=E0611,E0401

from tools import db  # pylint: disable=E0401

from ..models.usage_event import UsageEvent


def period_of(ts):
    """Denormalised 'YYYYMM' of the row timestamp."""
    return f"{ts:%Y%m}"


class Method:  # pylint: disable=E1101,R0903,W0201
    """ Method resource (self is the Module instance) """

    @web.method()
    def usage_write_event(self, row):
        """True when a row landed. At-least-once delivery is safe: a repeat of the same
        (idempotency_key, ts) is dropped by the partitioned unique index."""
        payload = dict(row)
        payload.setdefault("period", period_of(payload["ts"]))
        #
        statement = insert(UsageEvent).values(**payload).on_conflict_do_nothing(
            index_elements=["idempotency_key", "ts"],
        )
        #
        try:
            with db.engine.connect() as connection:
                result = connection.execute(statement)
                connection.commit()
            #
            return bool(result.rowcount)
        except:  # pylint: disable=W0702
            # Losing a row must never break the response the user is already reading
            log.exception("usage: failed to write usage_event")
            return False

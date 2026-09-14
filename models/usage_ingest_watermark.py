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

""" Ingest watermark model """

from sqlalchemy import BigInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from tools import db, config as c  # pylint: disable=E0401


class UsageIngestWatermark(db.Base):  # pylint: disable=R0903
    """How far the drainer has folded rows that bypass the write-behind queue.

    In Postgres, not Redis, so advancing it commits with the counter upsert it belongs to.
    """

    __tablename__ = "usage_ingest_watermark"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    last_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    __table_args__ = ({"schema": c.POSTGRES_SCHEMA},)

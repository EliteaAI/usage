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

""" Usage counter model """

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from tools import db, config as c  # pylint: disable=E0401


class UsageCounter(db.Base):  # pylint: disable=R0903
    """Rolled-up spend the enforcement gate reads. Sentinels avoid a nullable PK:
    user_id 0 = project aggregate, model_name '' = all models. Only this plugin reads them.

    Deliberate deviation from the issue text, which specifies NULL for both aggregate markers:
    a NULL column cannot take part in a primary key, so sentinels are the only workable form.
    """

    __tablename__ = "usage_counter"

    project_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    period_kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    model_name: Mapped[str] = mapped_column(String(256), primary_key=True, server_default="")

    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_usage_counter_period", "period_kind", "period_start"),
        {"schema": c.POSTGRES_SCHEMA},
    )

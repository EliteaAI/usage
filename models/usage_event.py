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

""" Usage event fact model """

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tools import db, config as c  # pylint: disable=E0401


class UsageEvent(db.Base):  # pylint: disable=R0903
    """One metered LLM or tool call. postgresql_partition_by lets create_all() provision the
    parent; child partitions come from usage_ensure_partitions().
    """

    __tablename__ = "usage_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # What the user launched; fixed for the whole run
    root_entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    root_entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    root_entity_version_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Which node inside the run made this call; tool rows only
    entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    entity_version_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    event_type: Mapped[str] = mapped_column(String(16), nullable=False)

    model_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    dialect: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    endpoint: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cache_creation_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0",
    )
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    # Integer micro-dollars, never float: money must not accumulate rounding error
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cost_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    token_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    tool_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Sparse extras only. Nothing a report filters on goes here.
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("uq_usage_event_idempotency", "idempotency_key", "ts", unique=True),
        Index("ix_usage_event_project_ts", "project_id", "ts"),
        Index("ix_usage_event_project_user_ts", "project_id", "user_id", "ts"),
        Index(
            "ix_usage_event_project_model_ts", "project_id", "model_name", "ts",
            postgresql_where=text("model_name IS NOT NULL"),
        ),
        Index(
            "ix_usage_event_project_root_ts",
            "project_id", "root_entity_type", "root_entity_id", "ts",
        ),
        Index(
            "ix_usage_event_run", "run_id",
            postgresql_where=text("run_id IS NOT NULL"),
        ),
        Index(
            "ix_usage_event_project_conversation_ts", "project_id", "conversation_id", "ts",
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
        Index(
            "ix_usage_event_project_tool_ts", "project_id", "tool_name", "ts",
            postgresql_where=text("tool_name IS NOT NULL"),
        ),
        {"schema": c.POSTGRES_SCHEMA, "postgresql_partition_by": "RANGE (ts)"},
    )

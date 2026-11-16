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

""" Agent/pipeline complexity: per-run scores, daily rollup, calibration state (#7553) """

from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tools import db, config as c  # pylint: disable=E0401


class EntityComplexityRun(db.Base):  # pylint: disable=R0903
    """One scored run. Partitioned by ts (the run's first event) so a resume re-scores in place."""

    __tablename__ = "entity_complexity_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, nullable=False)
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    root_entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    root_entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    root_entity_version_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    root_entity_project_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Raw inputs, kept so a new formula or weights version can rescore without usage_event
    peak_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_counts: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    cu_context: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    cu_llm: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    cu_tools: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    cu_total: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")

    formula_version: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_weights_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        # Partition key must be part of any unique index; ts is the run's stable first event
        Index("uq_entity_complexity_run", "project_id", "run_id", "ts", unique=True),
        Index(
            "ix_entity_complexity_run_entity_ts",
            "project_id", "root_entity_type", "root_entity_id", "ts",
        ),
        Index("ix_entity_complexity_run_project_ts", "project_id", "ts"),
        {"schema": c.POSTGRES_SCHEMA, "postgresql_partition_by": "RANGE (ts)"},
    )


class EntityComplexityDaily(db.Base):  # pylint: disable=R0903
    """Per-entity per-day rollup of run scores, rebuilt from run rows whenever one changes."""

    __tablename__ = "entity_complexity_daily"

    project_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    root_entity_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    root_entity_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    peak_input_tokens_sum: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0",
    )
    llm_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    tool_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cu_context_sum: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    cu_llm_sum: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    cu_tools_sum: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    cu_total_sum: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    cu_max: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # Run counts per log2 CU bucket, so medians/labels survive a threshold recalibration
    cu_histogram: Mapped[list] = mapped_column(ARRAY(Integer), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_entity_complexity_daily_project_day", "project_id", "day"),
        Index("ix_entity_complexity_daily_day", "day"),
        {"schema": c.POSTGRES_SCHEMA},
    )


class EntityComplexityState(db.Base):  # pylint: disable=R0903
    """Single row (id=1): scoring watermark plus the nightly thresholds and percentiles."""

    __tablename__ = "entity_complexity_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scored_through: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    thresholds: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # default | calibrated | pinned
    thresholds_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # p1..p99 breakpoints over runs and over per-entity median CU
    run_percentiles: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    entity_percentiles: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    sample_runs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sample_entities: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = ({"schema": c.POSTGRES_SCHEMA},)

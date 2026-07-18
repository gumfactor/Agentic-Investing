"""ORM models for versioned research identity (BUG-009 section 4, design plan
docs/plans/01b-research-validity-design.md).

Mirrors ``infra/db/migrations/versions/012_research_identity.py``. The
Alembic migration is the canonical Postgres schema (C2); this module is used
directly by application code and by tests (via ``Base.metadata.create_all``
against SQLite), following the same pattern as ``data/universe/models.py``.

A ``ResearchMethodology`` records every policy choice that determines whether
a research result is comparable to another: which universe import/version and
availability policy defined its cross-section, its signal-to-execution timing
policy, its score-feature and realized-return corporate-action availability
policies and action-source version, its return/adjustment policy, its
missing-data policy, and a code/config hash for the implementation that
produced it. A ``ResearchRun`` is one execution against a methodology plus a
data version (C7); ``research_run_id`` on ``signal_ic_stats``,
``factor_scores``, and ``alpha_scores`` ties every persisted metric back to
the exact methodology that produced it, and the unique constraints on those
tables now include ``research_run_id`` so a new run cannot silently UPSERT
over an old methodology's rows (section 4 item 1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ResearchMethodology(Base):
    """One versioned bundle of research-validity policy choices (section 4)."""

    __tablename__ = "research_methodologies"
    __table_args__ = (
        UniqueConstraint("name", name="uq_research_methodologies_name"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    universe_id: Mapped[Optional[str]] = mapped_column(Text)
    universe_import_policy: Mapped[str] = mapped_column(Text, nullable=False)
    timing_policy_id: Mapped[str] = mapped_column(Text, nullable=False)
    score_action_availability_policy: Mapped[str] = mapped_column(Text, nullable=False)
    realized_return_action_availability_policy: Mapped[str] = mapped_column(Text, nullable=False)
    action_source_version: Mapped[str] = mapped_column(Text, nullable=False)
    return_adjustment_policy: Mapped[str] = mapped_column(Text, nullable=False)
    missing_data_policy: Mapped[str] = mapped_column(Text, nullable=False)
    code_config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ResearchMethodology id={self.id} name={self.name!r}>"


class ResearchRun(Base):
    """One execution of a methodology against a specific data version (C7)."""

    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('legacy_provisional', 'candidate', 'active', 'superseded', 'rejected')",
            name="ck_research_runs_status",
        ),
        Index("ix_research_runs_methodology", "methodology_id"),
        # At most one active run per methodology — "explicitly approved
        # active run" lookups filter on is_active rather than assuming the
        # newest row (section 4 item 2). Postgres partial unique index;
        # emulated in SQLite tests via a plain (non-unique) index since
        # SQLite's partial-index syntax is not portably declared here.
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    methodology_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("research_methodologies.id", ondelete="RESTRICT"), nullable=False
    )
    data_version: Mapped[str] = mapped_column(Text, nullable=False)
    run_label: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="candidate")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    activated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    activated_by: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<ResearchRun id={self.id} methodology_id={self.methodology_id} "
            f"status={self.status!r} is_active={self.is_active}>"
        )

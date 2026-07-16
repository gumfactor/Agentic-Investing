"""ORM models for the point-in-time universe membership schema (BUG-008).

Mirrors ``infra/db/migrations/versions/009_universe_membership.py``. The
Alembic migration is the canonical Postgres schema (C2); this module is used
directly by application code and by tests (via ``Base.metadata.create_all``
against SQLite), following the same pattern as
``strategy_registry/models.py``.

One deliberate divergence from the migration: the Postgres-only
``EXCLUDE USING gist`` no-overlap constraint is not representable in
SQLAlchemy Core in a cross-dialect way, so it is not declared here. Overlap
rejection is enforced in Python by the import pipeline
(``data/universe/import_pipeline.py``) before any publish, and the DB
constraint in the migration is defense-in-depth for direct SQL writes against
a real Postgres instance.
"""

from __future__ import annotations

from datetime import date as date_
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
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


class UniverseImportBatch(Base):
    """One row per import run. Raw-source provenance + publish gate (§1.2)."""

    __tablename__ = "universe_import_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('staged', 'validated', 'published', 'rejected')",
            name="ck_universe_import_batches_status",
        ),
        CheckConstraint(
            "coverage_end IS NULL OR coverage_start IS NULL OR coverage_end >= coverage_start",
            name="ck_universe_import_batches_coverage_range",
        ),
        Index("ix_universe_import_batches_universe_status", "universe_id", "status"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    universe_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_version: Mapped[str] = mapped_column(Text, nullable=False)
    raw_artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    raw_checksum_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="staged")
    coverage_start: Mapped[Optional[date_]] = mapped_column(Date)
    coverage_end: Mapped[Optional[date_]] = mapped_column(Date)
    n_membership_rows: Mapped[Optional[int]] = mapped_column(Integer)
    n_symbol_history_rows: Mapped[Optional[int]] = mapped_column(Integer)
    rejected_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<UniverseImportBatch id={self.id} universe_id={self.universe_id!r} "
            f"status={self.status!r} coverage=[{self.coverage_start},{self.coverage_end}]>"
        )


class UniverseMembership(Base):
    """Canonical effective-dated constituent row (§1.1).

    Half-open interval: eligible when
    ``effective_start <= as_of_date < COALESCE(effective_end, infinity)``
    AND ``known_at <= observation_cutoff`` for that ``as_of_date``.
    """

    __tablename__ = "universe_membership"
    __table_args__ = (
        CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="ck_universe_membership_valid_range",
        ),
        CheckConstraint(
            "length(universe_id) > 0 AND length(ticker) > 0",
            name="ck_universe_membership_nonempty_ids",
        ),
        Index("ix_universe_membership_universe_ticker", "universe_id", "ticker"),
        Index("ix_universe_membership_universe_start", "universe_id", "effective_start"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    universe_id: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_symbol: Mapped[Optional[str]] = mapped_column(Text)
    effective_start: Mapped[date_] = mapped_column(Date, nullable=False)
    effective_end: Mapped[Optional[date_]] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    announced_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    known_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    source_version: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    import_batch_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("universe_import_batches.id", ondelete="RESTRICT")
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<UniverseMembership {self.universe_id}:{self.ticker} "
            f"[{self.effective_start},{self.effective_end})>"
        )


class SymbolHistory(Base):
    """Ticker-rename mapping (§1.1). Does not rewrite old ticks."""

    __tablename__ = "universe_symbol_history"
    __table_args__ = (
        UniqueConstraint(
            "universe_id", "old_ticker", "effective_date",
            name="uq_universe_symbol_history_change",
        ),
        CheckConstraint(
            "old_ticker <> new_ticker",
            name="ck_universe_symbol_history_distinct_tickers",
        ),
        Index("ix_universe_symbol_history_universe_old", "universe_id", "old_ticker"),
        Index("ix_universe_symbol_history_universe_new", "universe_id", "new_ticker"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    universe_id: Mapped[str] = mapped_column(Text, nullable=False)
    old_ticker: Mapped[str] = mapped_column(Text, nullable=False)
    new_ticker: Mapped[str] = mapped_column(Text, nullable=False)
    effective_date: Mapped[date_] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    known_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    source_version: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<SymbolHistory {self.old_ticker}->{self.new_ticker} @{self.effective_date}>"

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
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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
    # JSON text: {"tickers": [...], "reason": "..."} — operator
    # --exclude-tickers audit record (01B-2 fix round). NULL = no exclusions.
    excluded_tickers: Mapped[Optional[str]] = mapped_column(Text)
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
        CheckConstraint(
            "(effective_end IS NULL AND end_known_at IS NULL) "
            "OR (effective_end IS NOT NULL AND end_known_at IS NOT NULL)",
            name="ck_universe_membership_end_known_consistency",
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
    # Availability of the removal event closing this interval; NULL iff open.
    end_known_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
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


class UniverseEligibilityBatch(Base):
    """One row per nightly/backfill eligibility-attribute computation run.

    Mirrors ``infra/db/migrations/versions/013_universe_eligibility_attributes.py``
    (Roadmap 03A-4a, design plan §1.2 ``computation_batch_id`` provenance).
    Append-only: a correction publishes a new batch rather than mutating
    existing rows (C3-style discipline).
    """

    __tablename__ = "universe_eligibility_batches"
    __table_args__ = (
        CheckConstraint(
            "length(universe_id) > 0 AND length(code_version) > 0",
            name="ck_universe_eligibility_batches_nonempty_ids",
        ),
        Index(
            "ix_universe_eligibility_batches_universe_computed",
            "universe_id",
            "computed_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    universe_id: Mapped[str] = mapped_column(Text, nullable=False)
    code_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    n_attribute_rows: Mapped[Optional[int]] = mapped_column(Integer)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<UniverseEligibilityBatch id={self.id} universe_id={self.universe_id!r} "
            f"code_version={self.code_version!r}>"
        )


class UniverseEligibilityAttribute(Base):
    """Append-only, effective-dated PIT eligibility fact (§1.2).

    One row per ``(universe_id, ticker, attribute_name, effective_start)``.
    Half-open interval, same shape as :class:`UniverseMembership`. Exactly
    one of ``attribute_value_numeric``/``attribute_value_text`` is populated
    depending on ``attribute_name``'s declared type (numeric attributes:
    ``adv_usd_20d``, ``price_usd``; text attributes: ``security_type``).

    The Postgres-only ``EXCLUDE USING gist`` no-overlap constraint (scoped by
    ``computation_batch_id``, mirroring migration 009) is not representable
    in SQLAlchemy Core cross-dialect and is not declared here -- same
    deliberate divergence documented on :class:`UniverseMembership` above.
    """

    __tablename__ = "universe_eligibility_attributes"
    __table_args__ = (
        CheckConstraint(
            "length(universe_id) > 0 AND length(ticker) > 0 AND length(attribute_name) > 0",
            name="ck_universe_eligibility_attributes_nonempty_ids",
        ),
        CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="ck_universe_eligibility_attributes_valid_range",
        ),
        CheckConstraint(
            "(attribute_value_numeric IS NOT NULL AND attribute_value_text IS NULL) "
            "OR (attribute_value_numeric IS NULL AND attribute_value_text IS NOT NULL)",
            name="ck_universe_eligibility_attributes_exactly_one_value",
        ),
        CheckConstraint(
            "source_data_asof <= effective_start",
            name="ck_universe_eligibility_attributes_source_not_future",
        ),
        Index(
            "ix_universe_eligibility_attributes_universe_ticker_attr",
            "universe_id",
            "ticker",
            "attribute_name",
        ),
        Index(
            "ix_universe_eligibility_attributes_universe_attr_start",
            "universe_id",
            "attribute_name",
            "effective_start",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    universe_id: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    attribute_name: Mapped[str] = mapped_column(Text, nullable=False)
    attribute_value_numeric: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    attribute_value_text: Mapped[Optional[str]] = mapped_column(Text)
    effective_start: Mapped[date_] = mapped_column(Date, nullable=False)
    effective_end: Mapped[Optional[date_]] = mapped_column(Date)
    computed_from: Mapped[str] = mapped_column(Text, nullable=False)
    source_data_asof: Mapped[date_] = mapped_column(Date, nullable=False)
    computation_batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("universe_eligibility_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<UniverseEligibilityAttribute {self.universe_id}:{self.ticker} "
            f"{self.attribute_name}=[{self.effective_start},{self.effective_end})>"
        )

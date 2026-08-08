from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class StrategyDefinition(Base):
    """Research layer: one row per (strategy_id, config_hash)."""

    __tablename__ = "strategy_definitions"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id", "version", name="uq_strategy_definitions_version"
        ),
        CheckConstraint(
            "version > 0",
            name="ck_strategy_definitions_version_positive",
        ),
        CheckConstraint(
            "length(config_hash) = 64",
            name="ck_strategy_definitions_hash_length",
        ),
    )

    strategy_id: Mapped[str] = mapped_column(Text, primary_key=True)
    config_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    portfolio_method: Mapped[str | None] = mapped_column(Text)
    n_long: Mapped[int | None] = mapped_column(Integer)
    rebalance_frequency: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)
    # 04-4W (migration 017): which strategy_registry.fingerprint.
    # FINGERPRINT_ALGO_VERSION this row's config_hash was computed under.
    # NOT NULL with server_default '1' so existing pre-migration rows are
    # backfilled to 1 (the old algorithm, which included backtest.start_date/
    # end_date in the hash) automatically at migration time -- every row
    # written by the application from here on passes the current
    # FINGERPRINT_ALGO_VERSION (2) explicitly (see
    # strategy_registry.registry.StrategyRegistry.add_definition/register).
    # This makes the operator's back-compat waiver (docs/plans/04-identity-
    # evaluation-context-design.md) moot rather than merely declared: a
    # pre-v2 row is DISTINGUISHABLE from a v2 row at the schema level, so a
    # future recompute/migration (should this project ever approach live
    # capital) is a straightforward filtered UPDATE rather than an
    # undocumented assumption.
    fingerprint_algo_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    runs: Mapped[list[StrategyRun]] = relationship(
        "StrategyRun",
        primaryjoin=(
            "and_(StrategyDefinition.strategy_id == foreign(StrategyRun.strategy_id), "
            "StrategyDefinition.config_hash == foreign(StrategyRun.config_hash))"
        ),
        viewonly=True,
    )

    def __repr__(self) -> str:
        return f"<StrategyDefinition {self.strategy_id!r} v{self.version} {self.config_hash[:8]}…>"


class Strategy(Base):
    """Lifecycle layer: one row per strategy_id; pins canonical_config_hash."""

    __tablename__ = "strategies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_id", "canonical_config_hash"],
            ["strategy_definitions.strategy_id", "strategy_definitions.config_hash"],
            name="fk_strategies_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["supersedes_strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategies_supersedes",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('backtesting', 'paper', 'live', 'archived')",
            name="ck_strategies_status",
        ),
        # Partial unique indexes: at most one strategy in paper, one in live.
        # Defined here so Base.metadata.create_all() builds them (not just the migration).
        Index(
            "uix_strategies_one_paper",
            "status",
            unique=True,
            postgresql_where=sa.text("status = 'paper'"),
            sqlite_where=sa.text("status = 'paper'"),
        ),
        Index(
            "uix_strategies_one_live",
            "status",
            unique=True,
            postgresql_where=sa.text("status = 'live'"),
            sqlite_where=sa.text("status = 'live'"),
        ),
    )

    strategy_id: Mapped[str] = mapped_column(Text, primary_key=True)
    canonical_config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_family: Mapped[str | None] = mapped_column(Text)
    supersedes_strategy_id: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    activated_paper_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    activated_live_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    status_history: Mapped[list[StrategyStatusHistory]] = relationship(
        "StrategyStatusHistory",
        back_populates="strategy",
        order_by="StrategyStatusHistory.transitioned_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<Strategy {self.strategy_id!r} status={self.status!r}>"


class StrategyRun(Base):
    """Append-only experiment run record; links to definition, not lifecycle row."""

    __tablename__ = "strategy_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_id", "config_hash"],
            ["strategy_definitions.strategy_id", "strategy_definitions.config_hash"],
            name="fk_strategy_runs_definition",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "run_type IN ('unit', 'signal_ic', 'backtest', 'walk_forward', 'paper', 'live')",
            name="ck_strategy_runs_run_type",
        ),
        CheckConstraint(
            "status IN ('running', 'passed', 'failed', 'blocked')",
            name="ck_strategy_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        autoincrement=True,
        primary_key=True,
    )
    strategy_id: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    data_version: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    mlflow_run_id: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    def __repr__(self) -> str:
        return (
            f"<StrategyRun {self.strategy_id!r} {self.run_type!r} "
            f"status={self.status!r} id={self.id}>"
        )


class StrategyStatusHistory(Base):
    """Append-only audit trail of lifecycle transitions (C3)."""

    __tablename__ = "strategy_status_history"
    __table_args__ = (
        CheckConstraint(
            "to_status IN ('backtesting', 'paper', 'live', 'archived')",
            name="ck_strategy_status_history_to_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(
        Text,
        sa.ForeignKey("strategies.strategy_id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    operator_notes: Mapped[str | None] = mapped_column(Text)

    strategy: Mapped[Strategy] = relationship("Strategy", back_populates="status_history")

    def __repr__(self) -> str:
        return (
            f"<StrategyStatusHistory {self.strategy_id!r} "
            f"{self.from_status!r} → {self.to_status!r}>"
        )

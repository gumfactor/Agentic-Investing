from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import TIMESTAMP


class Base(DeclarativeBase):
    pass


class Strategy(Base):
    __tablename__ = "strategies"
    __table_args__ = (
        UniqueConstraint("strategy_id", name="uq_strategies_strategy_id"),
        CheckConstraint(
            "status IN ('backtesting', 'paper', 'live', 'archived')",
            name="ck_strategies_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(Text, nullable=False)
    config_path: Mapped[str] = mapped_column(Text, nullable=False)
    config_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    portfolio_method: Mapped[str | None] = mapped_column(Text)
    n_long: Mapped[int | None] = mapped_column(Integer)
    rebalance_frequency: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    activated_paper_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    activated_live_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    status_history: Mapped[list[StrategyStatusHistory]] = relationship(
        "StrategyStatusHistory",
        back_populates="strategy",
        order_by="StrategyStatusHistory.transitioned_at.desc()",
    )
    performance_snapshots: Mapped[list[StrategyPerformanceSnapshot]] = relationship(
        "StrategyPerformanceSnapshot",
        back_populates="strategy",
        order_by="StrategyPerformanceSnapshot.snapshot_date.desc()",
    )

    def __repr__(self) -> str:
        return f"<Strategy {self.strategy_id!r} status={self.status!r}>"


class StrategyStatusHistory(Base):
    __tablename__ = "strategy_status_history"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("strategies.strategy_id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    operator_notes: Mapped[str | None] = mapped_column(Text)

    strategy: Mapped[Strategy] = relationship("Strategy", back_populates="status_history")

    def __repr__(self) -> str:
        return (
            f"<StrategyStatusHistory {self.strategy_id!r} "
            f"{self.from_status!r} → {self.to_status!r}>"
        )


class StrategyPerformanceSnapshot(Base):
    __tablename__ = "strategy_performance_snapshots"
    __table_args__ = (
        CheckConstraint(
            "period_type IN ('backtest', 'paper', 'live')",
            name="ck_strategy_perf_period_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("strategies.strategy_id", ondelete="RESTRICT"),
        nullable=False,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_type: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    annualized_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    annualized_volatility: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    sharpe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    information_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    total_trades: Mapped[int | None] = mapped_column(Integer)
    data_version: Mapped[str | None] = mapped_column(Text)
    mlflow_run_id: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    strategy: Mapped[Strategy] = relationship(
        "Strategy", back_populates="performance_snapshots"
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyPerformanceSnapshot {self.strategy_id!r} "
            f"{self.period_type!r} {self.snapshot_date}>"
        )

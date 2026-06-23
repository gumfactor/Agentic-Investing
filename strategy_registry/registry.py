from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from strategy_registry.loader import compute_sha256, load_and_fingerprint
from strategy_registry.models import (
    Base,
    Strategy,
    StrategyPerformanceSnapshot,
    StrategyStatusHistory,
)

log = structlog.get_logger(__name__)

# ── Exceptions ───────────────────────────────────────────────────────────────


class StrategyRegistryError(Exception):
    pass


class StrategyAlreadyRegisteredError(StrategyRegistryError):
    pass


class StrategyNotFoundError(StrategyRegistryError):
    pass


class InvalidTransitionError(StrategyRegistryError):
    pass


class ConflictingActiveStrategyError(StrategyRegistryError):
    pass


class MissingOperatorNotesError(StrategyRegistryError):
    pass


class ConfigDriftError(StrategyRegistryError):
    pass


class MissingDataVersionError(StrategyRegistryError):
    pass


# ── Status and allowed transitions ──────────────────────────────────────────


class StrategyStatus(str, enum.Enum):
    BACKTESTING = "backtesting"
    PAPER = "paper"
    LIVE = "live"
    ARCHIVED = "archived"


_ALLOWED_TRANSITIONS: dict[StrategyStatus, set[StrategyStatus]] = {
    StrategyStatus.BACKTESTING: {StrategyStatus.PAPER, StrategyStatus.ARCHIVED},
    StrategyStatus.PAPER: {
        StrategyStatus.LIVE,
        StrategyStatus.BACKTESTING,
        StrategyStatus.ARCHIVED,
    },
    StrategyStatus.LIVE: {StrategyStatus.PAPER, StrategyStatus.ARCHIVED},
    StrategyStatus.ARCHIVED: set(),  # terminal
}

_SINGLE_ACTIVE_STATUSES = {StrategyStatus.PAPER, StrategyStatus.LIVE}


# ── PerformanceSnapshot dataclass ────────────────────────────────────────────


from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class PerformanceSnapshot:
    snapshot_date: date
    period_type: str  # 'backtest' | 'paper' | 'live'
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    annualized_return: Optional[Decimal] = None
    annualized_volatility: Optional[Decimal] = None
    sharpe_ratio: Optional[Decimal] = None
    max_drawdown: Optional[Decimal] = None
    information_ratio: Optional[Decimal] = None
    total_trades: Optional[int] = None
    data_version: Optional[str] = None  # required for period_type='backtest' (C7)
    mlflow_run_id: Optional[str] = None


# ── StrategyRegistry ─────────────────────────────────────────────────────────


class StrategyRegistry:
    def __init__(self, db_url: str) -> None:
        self._engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    def register(
        self,
        strategy_id: str,
        config_path: str,
        notes: Optional[str] = None,
    ) -> Strategy:
        """Register a new strategy from a YAML config file."""
        cfg = load_and_fingerprint(strategy_id, config_path)
        now = datetime.now(tz=timezone.utc)

        with Session(self._engine) as session:
            existing = session.scalar(
                select(Strategy).where(Strategy.strategy_id == strategy_id)
            )
            if existing is not None:
                raise StrategyAlreadyRegisteredError(
                    f"Strategy '{strategy_id}' is already registered (status='{existing.status}'). "
                    f"strategy_id values are permanent — create a new version (e.g. v2_...) instead."
                )

            strategy = Strategy(
                strategy_id=cfg.strategy_id,
                config_path=cfg.config_path,
                config_sha256=cfg.config_sha256,
                status=StrategyStatus.BACKTESTING,
                version=cfg.version,
                name=cfg.name,
                description=cfg.description,
                portfolio_method=cfg.portfolio_method,
                n_long=cfg.n_long,
                rebalance_frequency=cfg.rebalance_frequency,
                registered_at=now,
                notes=notes,
            )
            session.add(strategy)

            history = StrategyStatusHistory(
                strategy_id=strategy_id,
                from_status=None,
                to_status=StrategyStatus.BACKTESTING,
                transitioned_at=now,
                operator_notes=f"Initial registration. {notes or ''}".strip(),
            )
            session.add(history)
            session.commit()
            session.refresh(strategy)

            log.info(
                "strategy_registered",
                strategy_id=strategy_id,
                config_sha256=cfg.config_sha256,
            )
            return strategy

    def transition(
        self,
        strategy_id: str,
        to_status: StrategyStatus,
        operator_notes: Optional[str] = None,
    ) -> Strategy:
        """Transition a strategy to a new status."""
        if to_status == StrategyStatus.LIVE and not operator_notes:
            raise MissingOperatorNotesError(
                "operator_notes is required when transitioning to 'live'. "
                "Document the C8 clearance basis."
            )

        now = datetime.now(tz=timezone.utc)

        with Session(self._engine) as session:
            strategy = self._get_or_raise(session, strategy_id)
            from_status = StrategyStatus(strategy.status)

            allowed = _ALLOWED_TRANSITIONS[from_status]
            if to_status not in allowed:
                raise InvalidTransitionError(
                    f"Cannot transition '{strategy_id}' from '{from_status}' to "
                    f"'{to_status}'. Allowed: {sorted(s.value for s in allowed)}."
                )

            if to_status in _SINGLE_ACTIVE_STATUSES:
                conflict = session.scalar(
                    select(Strategy).where(
                        Strategy.status == to_status,
                        Strategy.strategy_id != strategy_id,
                    )
                )
                if conflict is not None:
                    raise ConflictingActiveStrategyError(
                        f"Strategy '{conflict.strategy_id}' is already in "
                        f"'{to_status}' status. Transition it away first."
                    )

            strategy.status = to_status
            if to_status == StrategyStatus.PAPER and strategy.activated_paper_at is None:
                strategy.activated_paper_at = now
            elif to_status == StrategyStatus.LIVE and strategy.activated_live_at is None:
                strategy.activated_live_at = now
            elif to_status == StrategyStatus.ARCHIVED:
                strategy.archived_at = now

            history = StrategyStatusHistory(
                strategy_id=strategy_id,
                from_status=from_status,
                to_status=to_status,
                transitioned_at=now,
                operator_notes=operator_notes,
            )
            session.add(history)
            session.commit()
            session.refresh(strategy)

            log.info(
                "strategy_transition",
                strategy_id=strategy_id,
                from_status=from_status,
                to_status=to_status,
            )
            return strategy

    def get(self, strategy_id: str) -> Strategy:
        with Session(self._engine) as session:
            return self._get_or_raise(session, strategy_id)

    def list(
        self,
        status: Optional[StrategyStatus] = None,
    ) -> list[Strategy]:
        with Session(self._engine) as session:
            q = select(Strategy)
            if status is not None:
                q = q.where(Strategy.status == status)
            return list(session.scalars(q.order_by(Strategy.registered_at)))

    def record_performance(
        self,
        strategy_id: str,
        snapshot: PerformanceSnapshot,
    ) -> StrategyPerformanceSnapshot:
        """Append a performance snapshot. data_version required for backtest (C7)."""
        if snapshot.period_type == "backtest" and not snapshot.data_version:
            raise MissingDataVersionError(
                "data_version is required for backtest performance snapshots (C7). "
                "Pass the MLflow manifest path."
            )

        now = datetime.now(tz=timezone.utc)

        with Session(self._engine) as session:
            self._get_or_raise(session, strategy_id)

            row = StrategyPerformanceSnapshot(
                strategy_id=strategy_id,
                snapshot_date=snapshot.snapshot_date,
                period_type=snapshot.period_type,
                period_start=snapshot.period_start,
                period_end=snapshot.period_end,
                annualized_return=snapshot.annualized_return,
                annualized_volatility=snapshot.annualized_volatility,
                sharpe_ratio=snapshot.sharpe_ratio,
                max_drawdown=snapshot.max_drawdown,
                information_ratio=snapshot.information_ratio,
                total_trades=snapshot.total_trades,
                data_version=snapshot.data_version,
                mlflow_run_id=snapshot.mlflow_run_id,
                recorded_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)

            log.info(
                "strategy_performance_recorded",
                strategy_id=strategy_id,
                period_type=snapshot.period_type,
                snapshot_date=str(snapshot.snapshot_date),
            )
            return row

    def verify_config_integrity(self, strategy_id: str) -> bool:
        """
        Re-hash the registered config file and compare against the stored SHA-256.
        Returns True if unchanged. Raises ConfigDriftError if the hash differs (C6).
        """
        with Session(self._engine) as session:
            strategy = self._get_or_raise(session, strategy_id)
            current_sha256 = compute_sha256(strategy.config_path)

            if current_sha256 != strategy.config_sha256:
                raise ConfigDriftError(
                    f"Config drift detected for '{strategy_id}'. "
                    f"Stored SHA-256: {strategy.config_sha256}. "
                    f"Current SHA-256: {current_sha256}. "
                    f"C6 requires creating a new versioned YAML instead of modifying "
                    f"one that has been used in a live/paper session."
                )

            log.info("config_integrity_verified", strategy_id=strategy_id)
            return True

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get_or_raise(self, session: Session, strategy_id: str) -> Strategy:
        strategy = session.scalar(
            select(Strategy).where(Strategy.strategy_id == strategy_id)
        )
        if strategy is None:
            raise StrategyNotFoundError(
                f"Strategy '{strategy_id}' not found in the registry."
            )
        return strategy

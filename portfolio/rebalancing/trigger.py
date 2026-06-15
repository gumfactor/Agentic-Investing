"""Rebalance trigger logic.

Determines whether a rebalance should be executed based on:
1. Calendar schedule (daily / weekly / monthly)
2. Signal drift — the portfolio has drifted enough from target weights
   that expected alpha loss exceeds transaction costs

Rule priority: circuit breaker (external) > calendar > drift.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


class RebalanceFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RebalanceTrigger:
    """Decides whether to rebalance on a given simulation or live date.

    Parameters
    ----------
    frequency:
        Calendar cadence — the minimum time between rebalances.
    drift_threshold:
        Trigger an early rebalance if the L1 drift between current and target
        weights exceeds this fraction (default 0.20 = 20 percentage points).
    min_holding_days:
        Never rebalance within this many trading days of the last rebalance.
    """

    def __init__(
        self,
        frequency: RebalanceFrequency | str = RebalanceFrequency.MONTHLY,
        drift_threshold: float = 0.20,
        min_holding_days: int = 5,
    ) -> None:
        self.frequency = RebalanceFrequency(frequency)
        self.drift_threshold = drift_threshold
        self.min_holding_days = min_holding_days
        self._last_rebalance_date: date | None = None
        self._trading_days_since: int = 0

    def should_rebalance(
        self,
        today: date,
        current_weights: pd.Series | None = None,
        target_weights: pd.Series | None = None,
        trading_days_since_last: int | None = None,
    ) -> tuple[bool, str]:
        """Return (should_rebalance, reason).

        Parameters
        ----------
        today:
            Current simulation/live date.
        current_weights:
            Current portfolio weights indexed by ticker.
        target_weights:
            Latest optimizer target weights indexed by ticker.
        trading_days_since_last:
            Override the internal counter (useful in backtesting).

        Returns
        -------
        (True, reason_string) if a rebalance is warranted; (False, reason) otherwise.
        """
        days_since = (
            trading_days_since_last
            if trading_days_since_last is not None
            else self._trading_days_since
        )

        # First rebalance always fires (no prior holdings to protect)
        if self._last_rebalance_date is None:
            return True, "first_rebalance"

        # Never rebalance before min_holding_days
        if days_since < self.min_holding_days:
            return False, f"min_holding_days not met ({days_since} < {self.min_holding_days})"

        # Calendar trigger
        if self._is_calendar_rebalance_day(today, days_since):
            return True, f"calendar:{self.frequency.value}"

        # Drift trigger (only if weights provided)
        if current_weights is not None and target_weights is not None:
            drift = _l1_drift(current_weights, target_weights)
            if drift > self.drift_threshold:
                logger.info(
                    "drift_trigger",
                    today=today.isoformat(),
                    drift=round(drift, 4),
                    threshold=self.drift_threshold,
                )
                return True, f"drift:{drift:.4f}"

        return False, "no_trigger"

    def record_rebalance(self, today: date) -> None:
        """Call after each actual rebalance to reset the internal counter."""
        self._last_rebalance_date = today
        self._trading_days_since = 0
        logger.info("rebalance_recorded", date=today.isoformat())

    def advance_day(self) -> None:
        """Increment internal trading-day counter (call once per trading day)."""
        self._trading_days_since += 1

    def _is_calendar_rebalance_day(self, today: date, days_since: int) -> bool:
        if self._last_rebalance_date is None:
            return True  # First rebalance always fires

        if self.frequency == RebalanceFrequency.DAILY:
            return True
        elif self.frequency == RebalanceFrequency.WEEKLY:
            return today.weekday() == 0 or days_since >= 5
        elif self.frequency == RebalanceFrequency.MONTHLY:
            return (
                today.month != self._last_rebalance_date.month
                or today.year != self._last_rebalance_date.year
            )
        return False  # unreachable


def _l1_drift(current: pd.Series, target: pd.Series) -> float:
    """One-way L1 turnover between current and target weights."""
    all_tickers = current.index.union(target.index)
    curr = current.reindex(all_tickers, fill_value=0.0)
    tgt = target.reindex(all_tickers, fill_value=0.0)
    return float((curr - tgt).abs().sum()) / 2.0

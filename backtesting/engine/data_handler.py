"""Point-in-time safe data access for the backtesting simulation.

DataHandler wraps pre-loaded DataFrames and enforces the temporal horizon:
only data whose observation date is <= simulation_date is visible to the engine.
No DB I/O occurs here — the caller loads data and passes DataFrames in.

This separation makes the engine fully testable without a database.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


class DataHandler:
    """PIT-safe data access for backtesting.

    Args:
        prices: Long-format DataFrame with columns ticker, date, open, high,
            low, close, volume. date column must be castable to datetime.date.
        alpha_scores: Long-format DataFrame with columns ticker, score_date,
            alpha_score (and optionally rank, universe_size, strategy_id).
            score_date is the date the score was computed; no future scores
            are visible on simulation dates before score_date.
        benchmark: Long-format DataFrame with columns date, close for a single
            benchmark ticker (e.g. SPY). Used to compute benchmark returns.
    """

    def __init__(
        self,
        prices: pd.DataFrame,
        alpha_scores: pd.DataFrame,
        benchmark: pd.DataFrame,
    ) -> None:
        self._prices = _normalise_date_col(prices.copy(), "date")
        self._alpha_scores = _normalise_date_col(alpha_scores.copy(), "score_date")
        self._benchmark = _normalise_date_col(benchmark.copy(), "date")

        _require_cols(self._prices, {"ticker", "date", "close"}, "prices")
        _require_cols(self._alpha_scores, {"ticker", "score_date", "alpha_score"}, "alpha_scores")
        _require_cols(self._benchmark, {"date", "close"}, "benchmark")

        self._prices["close"] = self._prices["close"].astype(float)
        self._benchmark["close"] = self._benchmark["close"].astype(float)

        self._sorted_dates: list[date] = sorted(self._prices["date"].unique())

    # ------------------------------------------------------------------
    # Core accessors
    # ------------------------------------------------------------------

    def get_close(self, sim_date: date) -> dict[str, float]:
        """Closing prices for all tickers on sim_date."""
        mask = self._prices["date"] == sim_date
        day = self._prices[mask]
        return dict(zip(day["ticker"], day["close"]))

    def get_latest_signals(self, sim_date: date) -> pd.DataFrame:
        """Most-recent alpha_score per ticker visible on sim_date.

        Enforces PIT: only scores with score_date <= sim_date are returned.
        Returns a DataFrame with columns ticker, score_date, alpha_score.
        """
        mask = self._alpha_scores["score_date"] <= sim_date
        visible = self._alpha_scores[mask]
        if visible.empty:
            return pd.DataFrame(columns=["ticker", "score_date", "alpha_score"])
        latest = (
            visible.sort_values("score_date")
            .groupby("ticker", sort=False)
            .last()
            .reset_index()[["ticker", "score_date", "alpha_score"]]
        )
        return latest

    def get_benchmark_return(self, sim_date: date) -> Optional[float]:
        """Daily return of the benchmark on sim_date. None if date not found."""
        bm = self._benchmark.sort_values("date")
        idx = bm.index[bm["date"] == sim_date].tolist()
        if not idx:
            return None
        pos = bm.index.get_loc(idx[0])
        if pos == 0:
            return None
        prev_close = bm.iloc[pos - 1]["close"]
        curr_close = bm.iloc[pos]["close"]
        if prev_close <= 0:
            return None
        return float(curr_close / prev_close - 1.0)

    def get_benchmark_returns_series(self, start: date, end: date) -> pd.Series:
        """Daily benchmark returns for [start, end]. Index = date."""
        bm = (
            self._benchmark[
                (self._benchmark["date"] >= start) & (self._benchmark["date"] <= end)
            ]
            .sort_values("date")
            .set_index("date")["close"]
        )
        return bm.pct_change().dropna()

    # ------------------------------------------------------------------
    # Date utilities
    # ------------------------------------------------------------------

    def trading_dates(self, start: date, end: date) -> list[date]:
        """All trading dates in [start, end] that appear in the price data."""
        return [d for d in self._sorted_dates if start <= d <= end]

    def rebalance_dates(
        self,
        start: date,
        end: date,
        frequency: str = "monthly",
    ) -> list[date]:
        """Trading dates where rebalancing should occur.

        frequency:
            'daily'   – every trading day
            'weekly'  – every 5th trading day starting from the first
            'monthly' – first trading day of each calendar month
        """
        all_dates = self.trading_dates(start, end)
        if not all_dates:
            return []
        if frequency == "daily":
            return all_dates
        if frequency == "weekly":
            return [d for i, d in enumerate(all_dates) if i % 5 == 0]
        if frequency == "monthly":
            result: list[date] = []
            last_month: Optional[int] = None
            for d in all_dates:
                if d.month != last_month:
                    result.append(d)
                    last_month = d.month
            return result
        raise ValueError(f"Unknown rebalance frequency: {frequency!r}")


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _normalise_date_col(df: pd.DataFrame, col: str) -> pd.DataFrame:
    df[col] = pd.to_datetime(df[col]).dt.date
    return df


def _require_cols(df: pd.DataFrame, required: set[str], name: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{name} DataFrame missing required columns: {missing}")

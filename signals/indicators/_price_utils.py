"""Shared helpers for price-based factor modules."""

from __future__ import annotations

import numpy as np
import pandas as pd

_TRADING_DAYS_PER_YEAR = 252


def validate_prices(prices: pd.DataFrame) -> None:
    required = {"ticker", "date", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices DataFrame missing required columns: {missing}")
    if prices.empty:
        raise ValueError("prices DataFrame is empty")


def to_wide(prices: pd.DataFrame) -> pd.DataFrame:
    """Pivot long-format (ticker, date, close) to wide (index=date, columns=ticker)."""
    wide = (
        prices[["ticker", "date", "close"]]
        .assign(close=lambda df: df["close"].astype(float))
        .pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
    )
    wide.columns.name = None
    return wide


def to_long(wide: pd.DataFrame, score_col: str) -> pd.DataFrame:
    """Melt a wide score DataFrame back to long format, dropping NaN rows."""
    return (
        wide.reset_index()
        .melt(id_vars="date", var_name="ticker", value_name=score_col)
        .dropna(subset=[score_col])
        .sort_values(["date", "ticker"])
        .reset_index(drop=True)
    )


def cross_sectional_zscore(wide: pd.DataFrame) -> pd.DataFrame:
    """Z-score each row cross-sectionally (per date, across tickers)."""
    row_mean = wide.mean(axis=1)
    row_std = wide.std(axis=1, ddof=1)
    return wide.sub(row_mean, axis=0).div(row_std, axis=0)


def compute_sma(wide: pd.DataFrame, window: int, min_obs_fraction: float = 0.7) -> pd.DataFrame:
    """Simple moving average."""
    return wide.rolling(window=window, min_periods=int(window * min_obs_fraction)).mean()


def compute_ema(wide: pd.DataFrame, span: int) -> pd.DataFrame:
    """Exponential moving average."""
    return wide.ewm(span=span, adjust=False).mean()


def price_return(wide: pd.DataFrame, lookback: int, skip: int = 0) -> pd.DataFrame:
    """(Price[t - skip] / Price[t - skip - lookback]) - 1."""
    return wide.shift(skip) / wide.shift(skip + lookback) - 1

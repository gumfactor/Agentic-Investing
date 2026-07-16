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


def daily_return(wide: pd.DataFrame) -> pd.DataFrame:
    """Elementwise daily percent-change return with missing prices preserved as NaN.

    BUG-010 (missing-data return policy): this is the single sanctioned way to
    turn a wide price matrix into a daily return matrix in the indicator
    library. It always calls ``pct_change(fill_method=None)`` so pandas never
    forward-fills a missing session's price before differencing — a gap in
    the price series yields ``NaN`` for that day's return, never a fabricated
    zero. Direct ``wide.pct_change(fill_method=None)`` calls remain acceptable
    where importing this helper would be awkward (e.g. outside
    ``signals/indicators``), provided they use the same explicit
    ``fill_method=None`` argument.

    Validation: any *present* (non-NaN) price must be finite and strictly
    positive. A non-positive or infinite price is a data-quality defect, not
    a "missing" observation, so it is rejected outright rather than silently
    turned into a NaN return.

    Valid-return counting: the returned matrix has one fewer valid
    observation per column than the input has valid prices, because the
    first return in any run of consecutive valid prices is undefined
    (pct_change needs two adjacent valid prices). Callers computing a
    rolling statistic over a lookback of N returns must use
    ``rolling(window=N, min_periods=N)`` (see ``require_full_window`` /
    ``rolling_valid_count`` below for calculations where NaN does not
    propagate automatically through arithmetic, e.g. cumulative sums or
    boolean masks) so a window spanning a gap is suppressed by default
    rather than computed from fewer, non-contiguous observations.
    """
    numeric = wide.to_numpy(dtype=float, copy=False)
    present = ~np.isnan(numeric)
    if present.any():
        present_values = numeric[present]
        if not np.isfinite(present_values).all():
            raise ValueError("daily_return received non-finite (inf) price values")
        if (present_values <= 0).any():
            raise ValueError("daily_return received non-positive price values")
    return wide.pct_change(fill_method=None)


def rolling_valid_count(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    """Count of non-missing (non-NaN) return observations in each trailing window.

    Used to gate derived statistics (cumulative sums, sign/boolean masks,
    shift-based deltas) where a missing return does not automatically
    propagate as NaN through the downstream arithmetic, so a plain
    ``min_periods`` on the derived series would not detect a gap.
    """
    return returns.notna().rolling(window=window, min_periods=1).sum()


def require_full_window(value: pd.DataFrame, returns: pd.DataFrame, window: int) -> pd.DataFrame:
    """Suppress `value` wherever its trailing `window` of `returns` contains a gap.

    Implements the default missing-data policy (BUG-010 §3.1): a lookback of
    N returns requires N valid, contiguous returns in the window, not merely
    N calendar rows. Use this for statistics built from cumulative sums,
    signs, or masks of `returns` (e.g. OBV, PVT) where a missing return does
    not itself turn the derived quantity into NaN.
    """
    valid_count = rolling_valid_count(returns, window)
    return value.where(valid_count >= window)

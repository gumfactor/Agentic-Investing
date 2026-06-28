"""Low-volatility composite signal.

Blends three realized-volatility windows (21d, 63d, 252d) into a single
lowvol_score. Low volatility relative to peers = positive score, consistent
with the rest of the signal library where higher = stronger long candidate.

Optionally incorporates rolling 252-day beta when market_prices are supplied;
beta is included in the output but excluded from the composite (it captures a
different risk dimension).

Methodology
-----------
The low-volatility anomaly (Blitz & van Vliet 2007, Baker et al. 2011)
observes that low-risk stocks deliver higher risk-adjusted returns than
high-risk stocks.

Individual vol measures (vol_21d, vol_63d, vol_252d, beta_252d) are also
returned so strategies can reference them independently.

Annualisation
~~~~~~~~~~~~~
Daily log-return std × sqrt(252).

Point-in-time safety
~~~~~~~~~~~~~~~~~~~~
Only ``date`` and ``close`` are consumed from the prices DataFrame.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_TRADING_DAYS_PER_YEAR = 252

_VOL_WINDOWS: dict[str, int] = {
    "vol_21d":  21,
    "vol_63d":  63,
    "vol_252d": 252,
}

_MIN_OBS_FRACTION = 0.7
_BETA_WINDOW = 252


def compute_lowvol_scores(
    prices: pd.DataFrame,
    market_prices: Optional[pd.DataFrame] = None,
    vol_windows: Optional[dict[str, int]] = None,
    beta_window: int = _BETA_WINDOW,
    min_obs_fraction: float = _MIN_OBS_FRACTION,
) -> pd.DataFrame:
    """Compute cross-sectional low-volatility scores.

    Args:
        prices: Long-format DataFrame with columns ``ticker``, ``date``,
            ``close``.  Multiple tickers expected.
        market_prices: Optional single-ticker long-format DataFrame with
            the market index (e.g. SPY).  Used for beta; if None, ``beta_252d``
            is omitted from output.
        vol_windows: Mapping of column name → lookback in trading-day rows.
            Defaults to 21 / 63 / 252-day windows.
        beta_window: Lookback for rolling beta computation (rows).
        min_obs_fraction: Minimum fraction of window rows that must have
            valid log-returns for a score to be assigned.

    Returns:
        Long-format DataFrame sorted by (date, ticker) with columns:
            ``ticker``, ``date``,
            ``vol_21d``, ``vol_63d``, ``vol_252d``,   (annualised %)
            ``beta_252d``                               (if market_prices supplied),
            ``lowvol_score``                            (higher = less volatile)
    """
    if vol_windows is None:
        vol_windows = _VOL_WINDOWS

    _validate_input(prices)

    wide = _to_wide(prices)
    log_ret = np.log(wide / wide.shift(1))

    long_frames: list[pd.DataFrame] = []

    for col_name, window in vol_windows.items():
        min_obs = int(window * min_obs_fraction)

        rolling_std = log_ret.rolling(window=window, min_periods=min_obs).std()
        annualised_vol = rolling_std * np.sqrt(_TRADING_DAYS_PER_YEAR)

        z = _cross_sectional_zscore(annualised_vol)

        long_frames.append(
            z.reset_index()
            .melt(id_vars="date", var_name="ticker", value_name=col_name)
            .set_index(["date", "ticker"])
        )

    if market_prices is not None:
        _validate_input(market_prices)
        mkt_wide = _to_wide(market_prices)
        mkt_ret = np.log(mkt_wide / mkt_wide.shift(1))
        mkt_ret = mkt_ret.reindex(log_ret.index)

        beta_z = _compute_beta(log_ret, mkt_ret, beta_window, min_obs_fraction)
        long_frames.append(
            beta_z.reset_index()
            .melt(id_vars="date", var_name="ticker", value_name="beta_252d")
            .set_index(["date", "ticker"])
        )

    if not long_frames:
        cols = ["ticker", "date"] + list(vol_windows) + ["lowvol_score"]
        if market_prices is not None:
            cols.insert(-1, "beta_252d")
        return pd.DataFrame(columns=cols)

    result = long_frames[0]
    for frame in long_frames[1:]:
        result = result.join(frame, how="outer")
    result = result.reset_index()

    vol_cols = list(vol_windows.keys())
    available_vol = [c for c in vol_cols if c in result.columns]

    composite_vol_z = result[available_vol].mean(axis=1, skipna=True)
    result["lowvol_score"] = -composite_vol_z

    def _restandardise(df: pd.DataFrame, col: str) -> pd.Series:
        grp = df.groupby("date")[col]
        return (df[col] - grp.transform("mean")) / grp.transform("std")

    result["lowvol_score"] = _restandardise(result, "lowvol_score")
    result = result.dropna(subset=available_vol, how="all")
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "lowvol_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        windows=vol_cols,
        include_beta=market_prices is not None,
    )
    return result


def _to_wide(prices: pd.DataFrame) -> pd.DataFrame:
    wide = (
        prices[["ticker", "date", "close"]]
        .assign(close=lambda df: df["close"].astype(float))
        .pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
    )
    wide.columns.name = None
    return wide


def _cross_sectional_zscore(wide: pd.DataFrame) -> pd.DataFrame:
    row_mean = wide.mean(axis=1)
    row_std = wide.std(axis=1, ddof=1)
    return wide.sub(row_mean, axis=0).div(row_std, axis=0)


def _compute_beta(
    stock_ret: pd.DataFrame,
    mkt_ret: pd.DataFrame,
    window: int,
    min_obs_fraction: float,
) -> pd.DataFrame:
    """Rolling OLS beta for each ticker vs a single market return series."""
    min_obs = int(window * min_obs_fraction)

    mkt_series = mkt_ret.squeeze()
    if isinstance(mkt_series, pd.DataFrame):
        mkt_series = mkt_series.iloc[:, 0]

    result_cols = {}
    for ticker in stock_ret.columns:
        s = stock_ret[ticker]
        combined = pd.concat([s, mkt_series], axis=1, join="inner")
        combined.columns = ["stock", "market"]

        rolling_cov = (
            combined["stock"]
            .rolling(window=window, min_periods=min_obs)
            .cov(combined["market"])
        )
        rolling_var = (
            combined["market"]
            .rolling(window=window, min_periods=min_obs)
            .var()
        )
        beta = rolling_cov / rolling_var
        result_cols[ticker] = beta.reindex(stock_ret.index)

    beta_wide = pd.DataFrame(result_cols)
    return _cross_sectional_zscore(beta_wide)


def _validate_input(prices: pd.DataFrame) -> None:
    required = {"ticker", "date", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices DataFrame missing required columns: {missing}")
    if prices.empty:
        raise ValueError("prices DataFrame is empty")

"""Low-volatility factor.

Computes cross-sectional low-volatility scores from daily OHLCV data.

Methodology
-----------
The low-volatility anomaly (Blitz & van Vliet 2007, Baker et al. 2011)
observes that low-risk stocks deliver higher risk-adjusted returns than
high-risk stocks.  Scores here reflect *low* volatility as a positive
signal, consistent with the rest of the factor library where a higher
score = stronger long candidate.

Metrics computed
~~~~~~~~~~~~~~~~
vol_21d   Realised volatility, 21-day window  (annualised, %)
vol_63d   Realised volatility, 63-day window  (annualised, %)
vol_252d  Realised volatility, 252-day window (annualised, %)
beta_252d Rolling 252-day market beta (requires market_prices; NaN if absent)

Composite score
~~~~~~~~~~~~~~~
lowvol_score = cross-sectional z-score of *negative* composite volatility.
A stock less volatile than its peers receives a positive score.

Composite volatility = equal-weight mean of available vol window z-scores
(not negated yet), then the whole thing is negated at the end so that the
output sign convention matches the rest of the factor library.

Annualisation
~~~~~~~~~~~~~
Daily log-return std × sqrt(252).  Log returns used instead of simple
returns to avoid compounding artefacts in long windows.

Point-in-time safety
~~~~~~~~~~~~~~~~~~~~
Only ``date`` and ``close`` are consumed from the prices DataFrame (plus
the optional market series).  No future information enters.

Survivorship bias
~~~~~~~~~~~~~~~~~
Same caveat as momentum.py — Phase 1 uses current-membership universe.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_TRADING_DAYS_PER_YEAR = 252

# Volatility lookback windows (trading-day rows)
_VOL_WINDOWS: dict[str, int] = {
    "vol_21d":  21,
    "vol_63d":  63,
    "vol_252d": 252,
}

# Minimum fraction of window that must have valid returns for a score
_MIN_OBS_FRACTION = 0.7

# Beta lookback
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
            the market index (e.g. SPY) in the same ``ticker``/``date``/
            ``close`` schema.  Used for beta; if None, ``beta_252d`` is
            omitted from output.
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

        Rows where no volatility score can be computed are dropped.
    """
    if vol_windows is None:
        vol_windows = _VOL_WINDOWS

    _validate_input(prices)

    # Wide price matrix: index=date, columns=ticker
    wide = _to_wide(prices)

    # Log-returns (avoids compounding distortion over long windows)
    log_ret = np.log(wide / wide.shift(1))

    long_frames: list[pd.DataFrame] = []

    # ── Realised volatility windows ───────────────────────────────────────
    for col_name, window in vol_windows.items():
        min_obs = int(window * min_obs_fraction)

        rolling_std = log_ret.rolling(window=window, min_periods=min_obs).std()
        annualised_vol = rolling_std * np.sqrt(_TRADING_DAYS_PER_YEAR)

        # Cross-sectional z-score (per date, across tickers)
        z = _cross_sectional_zscore(annualised_vol)

        long_frames.append(
            z.reset_index()
            .melt(id_vars="date", var_name="ticker", value_name=col_name)
            .set_index(["date", "ticker"])
        )

    # ── Rolling beta ──────────────────────────────────────────────────────
    if market_prices is not None:
        _validate_input(market_prices)
        mkt_wide = _to_wide(market_prices)
        # Align to the same dates as the universe prices
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

    # Outer-join all metric frames on (date, ticker)
    result = long_frames[0]
    for frame in long_frames[1:]:
        result = result.join(frame, how="outer")
    result = result.reset_index()

    # ── Composite low-vol score ───────────────────────────────────────────
    # Composite = mean of vol window z-scores (beta excluded from composite;
    # it captures a different risk dimension).
    vol_cols = list(vol_windows.keys())
    available_vol = [c for c in vol_cols if c in result.columns]

    # Mean of z-scores per row, then negate (low vol → positive score)
    composite_vol_z = result[available_vol].mean(axis=1, skipna=True)
    result["lowvol_score"] = -composite_vol_z

    # Re-standardise the composite so it's centred and unit-variance per date
    def _restandardise(df: pd.DataFrame, col: str) -> pd.Series:
        grp = df.groupby("date")[col]
        return (df[col] - grp.transform("mean")) / grp.transform("std")

    result["lowvol_score"] = _restandardise(result, "lowvol_score")

    # Drop rows where no vol score was computable
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_wide(prices: pd.DataFrame) -> pd.DataFrame:
    """Pivot long-format prices to wide (index=date, columns=ticker)."""
    wide = (
        prices[["ticker", "date", "close"]]
        .assign(close=lambda df: df["close"].astype(float))
        .pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
    )
    wide.columns.name = None
    return wide


def _cross_sectional_zscore(wide: pd.DataFrame) -> pd.DataFrame:
    """Z-score each row of a wide DataFrame cross-sectionally."""
    row_mean = wide.mean(axis=1)
    row_std = wide.std(axis=1, ddof=1)
    return wide.sub(row_mean, axis=0).div(row_std, axis=0)


def _compute_beta(
    stock_ret: pd.DataFrame,
    mkt_ret: pd.DataFrame,
    window: int,
    min_obs_fraction: float,
) -> pd.DataFrame:
    """Rolling OLS beta for each ticker vs a single market return series.

    Beta = Cov(stock, market) / Var(market), computed via rolling window.
    Returns a wide DataFrame of beta values (index=date, columns=ticker).
    """
    min_obs = int(window * min_obs_fraction)

    # Market return is a single-column DataFrame; squeeze to Series
    mkt_series = mkt_ret.squeeze()
    if isinstance(mkt_series, pd.DataFrame):
        mkt_series = mkt_series.iloc[:, 0]

    result_cols = {}
    for ticker in stock_ret.columns:
        s = stock_ret[ticker]
        # Align and drop rows where either is NaN before rolling
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

    # Cross-sectional z-score so beta is on the same scale as vol metrics
    return _cross_sectional_zscore(beta_wide)


def _validate_input(prices: pd.DataFrame) -> None:
    required = {"ticker", "date", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices DataFrame missing required columns: {missing}")
    if prices.empty:
        raise ValueError("prices DataFrame is empty")

"""Momentum composite signal.

Blends four lookback-window momentum returns (1m, 3m, 6m, 12m) into a single
momentum_score via equal-weight cross-sectional z-score averaging.

Methodology (Jegadeesh & Titman, 1993 / standard quant practice)
-----------------------------------------------------------------
1. For each lookback window L (1 M, 3 M, 6 M, 12 M), compute the
   total return over [t - L - 1 M, t - 1 M].  The final month is
   skipped to avoid short-term mean-reversion contamination.
2. Cross-sectionally z-score each window's return on each date.
3. momentum_score = equal-weight average of all available window z-scores.

Individual window scores (mom_1m, mom_3m, mom_6m, mom_12m) are also returned
in the output DataFrame so strategies can reference them independently.

Point-in-time safety
--------------------
Only ``date`` and ``close`` columns are consumed.  No future information
enters the calculation.

Units
-----
- Input ``close``: any consistent price unit (Decimal or float accepted).
- Output scores: dimensionless z-scores, centred at 0.

Survivorship bias note
----------------------
Phase 1 uses a current-membership S&P 500 universe, so scores computed
over historical windows carry survivorship bias.  Phase 2 will replace
the universe source with point-in-time constituent history.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_SKIP_DAYS = 21       # ~1 trading month excluded at the near end
_WINDOWS: dict[str, int] = {
    "mom_1m":   21,
    "mom_3m":   63,
    "mom_6m":  126,
    "mom_12m": 252,
}


def compute_momentum_scores(
    prices: pd.DataFrame,
    windows: Optional[dict[str, int]] = None,
    skip_days: int = _SKIP_DAYS,
    min_obs_fraction: float = 0.7,
    eligibility: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Compute cross-sectional momentum scores.

    Args:
        prices: Long-format DataFrame with columns ``ticker``, ``date``,
            ``close``.  Multiple tickers expected.
        windows: Mapping of score column name → lookback in trading-day rows.
            Defaults to the four standard windows (1 M, 3 M, 6 M, 12 M).
        skip_days: Rows at the near end of each window excluded from the
            return (short-term reversal buffer).  Default = 21.
        min_obs_fraction: Fraction of the lookback window that must have
            valid prices for a score to be assigned vs NaN.
        eligibility: optional long-format DataFrame with ``ticker``/``date``
            columns listing the ELIGIBLE (ticker, date) pairs — the
            point-in-time scoring cross-section (BUG-008 / Codex PR #34 P1).
            Raw window returns still use each ticker's full price history
            (lookbacks are unaffected), but ineligible cells are masked to
            NaN BEFORE the cross-sectional mean/std, so a non-member's
            return can never shift members' z-scores. Dates present in
            ``prices`` but absent from ``eligibility`` are fully masked
            (fail closed). ``None`` keeps the legacy behavior: every priced
            ticker is in the cross-section (provisional per the design
            plan).

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``mom_1m``, ``mom_3m``, ``mom_6m``,
            ``mom_12m``, ``momentum_score``

        ``momentum_score`` is the equal-weight composite of the available
        window z-scores for each (ticker, date).  Rows where no window
        score is available are dropped.
    """
    if windows is None:
        windows = _WINDOWS

    _validate_input(prices)

    wide = (
        prices[["ticker", "date", "close"]]
        .assign(close=lambda df: df["close"].astype(float))
        .pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
    )
    wide.columns.name = None

    eligible_mask: Optional[pd.DataFrame] = None
    if eligibility is not None:
        from signals.composites._eligibility import build_wide_eligibility_mask

        eligible_mask = build_wide_eligibility_mask(eligibility, wide.index, wide.columns)

    long_frames: list[pd.DataFrame] = []

    for col_name, lookback in windows.items():
        total_window = lookback + skip_days
        min_obs = int(lookback * min_obs_fraction)

        near_price = wide.shift(skip_days)
        far_price = wide.shift(total_window)

        obs_in_window = (
            wide.rolling(window=lookback, min_periods=1).count().shift(skip_days)
        )

        raw_return = near_price / far_price - 1.0
        raw_return[obs_in_window < min_obs] = np.nan
        if eligible_mask is not None:
            # PIT cross-section: ineligible cells leave the row statistics
            # entirely, not merely the output rows.
            raw_return = raw_return.where(eligible_mask)

        row_mean = raw_return.mean(axis=1)
        row_std = raw_return.std(axis=1, ddof=1)
        z = raw_return.sub(row_mean, axis=0).div(row_std, axis=0)

        melted = (
            z.reset_index()
            .melt(id_vars="date", var_name="ticker", value_name=col_name)
        )
        long_frames.append(melted.set_index(["date", "ticker"]))

    if not long_frames:
        return pd.DataFrame(
            columns=["ticker", "date"] + list(windows) + ["momentum_score"]
        )

    result = long_frames[0]
    for frame in long_frames[1:]:
        result = result.join(frame, how="outer")
    result = result.reset_index()

    window_cols = list(windows.keys())
    result["momentum_score"] = result[window_cols].mean(axis=1, skipna=True)
    result = result.dropna(subset=window_cols, how="all")
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "momentum_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        windows=list(windows.keys()),
    )
    return result


def rank_by_momentum(
    scores: pd.DataFrame,
    score_col: str = "momentum_score",
    n_long: int = 50,
    n_short: int = 50,
) -> pd.DataFrame:
    """Rank tickers by momentum score and label top/bottom buckets.

    Args:
        scores: Output of ``compute_momentum_scores``.
        score_col: Column to rank on.
        n_long: Number of top-ranked tickers per date to label 'long'.
        n_short: Number of bottom-ranked tickers per date to label 'short'.

    Returns:
        Input DataFrame with two additional columns:
            ``rank`` (1 = highest score on that date),
            ``bucket`` ('long' | 'short' | None).
    """
    if score_col not in scores.columns:
        raise ValueError(f"score_col {score_col!r} not found in DataFrame")

    out = scores.copy()
    out["rank"] = (
        out.groupby("date")[score_col]
        .rank(ascending=False, method="first", na_option="bottom")
        .astype("Int64")
    )
    out["bucket"] = None
    out.loc[out["rank"] <= n_long, "bucket"] = "long"

    max_rank = out.groupby("date")["rank"].transform("max")
    out.loc[out["rank"] > (max_rank - n_short), "bucket"] = "short"

    return out


def _validate_input(prices: pd.DataFrame) -> None:
    required = {"ticker", "date", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices DataFrame missing required columns: {missing}")
    if prices.empty:
        raise ValueError("prices DataFrame is empty")

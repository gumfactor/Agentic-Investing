"""Vol trend slope factor (63-day).

Fits a linear regression to the series of 21-day rolling realized vol values
over the past 63 days. Positive slope = volatility is trending upward (risk rising).
Negative slope = volatility is declining (conditions calming).
Normalised by current vol level for cross-sectional comparability.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return

logger = structlog.get_logger(__name__)

_INNER = 21
_INNER_MIN = 21  # full window (BUG-010): vol_21 itself requires 21 contiguous, non-gapped returns
_OUTER = 63
# Documented exception to the BUG-010 full-window default (see
# docs/plans/01b1-pct-change-inventory.md): the OLS trend fit over the
# already-gap-free-by-construction vol_21 series is deliberately robust to a
# limited number of missing/NaN points via its own internal
# mask.sum() >= 20 threshold inside _vol_slope, rather than requiring the
# full 63-point window to be present. min_periods governs only whether
# _vol_slope is *invoked* at all, so it is set to the same 20-point
# threshold the function itself enforces — a single missing bar NaNs two
# returns and therefore ~22 consecutive vol_21 values, leaving ~41 valid
# points in an affected 63-point outer window; a higher min_periods (the
# pre-01B-1 44) would starve the documented tolerance and suppress the
# slope around ordinary one-day gaps (PR #32 Codex P2).
# See test_vol_trend_slope_63d_gap_tolerance for coverage.
_OUTER_MIN = 20


def _vol_slope(x: np.ndarray) -> float:
    mask = ~np.isnan(x)
    if mask.sum() < 20:
        return np.nan
    t = np.arange(len(x), dtype=float)[mask]
    slope, _ = np.polyfit(t, x[mask], 1)
    return slope


def compute_vol_trend_slope_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of linear slope of 21d vol over 63 days, normalised by vol level."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = daily_return(wide)
    vol_21 = daily_ret.rolling(_INNER, min_periods=_INNER_MIN).std() * np.sqrt(252)
    slope = vol_21.rolling(_OUTER, min_periods=_OUTER_MIN).apply(_vol_slope, raw=True)
    # Normalise by current vol so the slope is dimensionless
    norm_slope = slope / vol_21.where(vol_21 > 0)
    z = cross_sectional_zscore(norm_slope)
    result = to_long(z, "vol_trend_slope_63d_score")
    logger.info("vol_trend_slope_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

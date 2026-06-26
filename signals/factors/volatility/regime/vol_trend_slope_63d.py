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
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_OUTER = 63
_OUTER_MIN = 44


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
    daily_ret = wide.pct_change()
    vol_21 = daily_ret.rolling(21, min_periods=15).std() * np.sqrt(252)
    slope = vol_21.rolling(_OUTER, min_periods=_OUTER_MIN).apply(_vol_slope, raw=True)
    # Normalise by current vol so the slope is dimensionless
    norm_slope = slope / vol_21.where(vol_21 > 0)
    z = cross_sectional_zscore(norm_slope)
    result = to_long(z, "vol_trend_slope_63d_score")
    logger.info("vol_trend_slope_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

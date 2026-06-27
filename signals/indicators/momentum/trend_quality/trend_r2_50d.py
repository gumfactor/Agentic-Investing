"""50-day trend R² factor.

R² of a linear regression of log-price on time over the past 50 days.
High R² = price moved in a straight line (clean trend, low noise).
Combined with a positive slope sign: high R² uptrends score highest.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 50
_MIN_PERIODS = 35


def _signed_r2(x: np.ndarray) -> float:
    """R² of log-price vs time, signed by the slope direction."""
    if np.sum(~np.isnan(x)) < _MIN_PERIODS:
        return np.nan
    t = np.arange(len(x), dtype=float)
    log_p = np.log(x)
    mask = ~np.isnan(log_p)
    if mask.sum() < _MIN_PERIODS:
        return np.nan
    t_m, lp_m = t[mask], log_p[mask]
    slope, intercept = np.polyfit(t_m, lp_m, 1)
    fitted = slope * t_m + intercept
    ss_res = np.sum((lp_m - fitted) ** 2)
    ss_tot = np.sum((lp_m - lp_m.mean()) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return r2 if slope > 0 else -r2


def compute_trend_r2_50d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of signed R² of 50-day log-price trend.
    Positive = clean uptrend; negative = clean downtrend."""
    validate_prices(prices)
    wide = to_wide(prices)
    signed_r2 = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).apply(_signed_r2, raw=True)
    z = cross_sectional_zscore(signed_r2)
    result = to_long(z, "trend_r2_50d_score")
    logger.info("trend_r2_50d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

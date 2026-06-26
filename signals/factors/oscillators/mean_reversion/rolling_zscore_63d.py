"""63-day rolling Z-score factor.

Z = (Close - mean(63d)) / std(63d).
Measures how many standard deviations price is above its quarterly mean.
Positive = above quarterly average; higher = more extended.
Dual-use: positive weight = momentum use (trending above mean);
          negative weight = mean-reversion use (sell what's overextended).
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 44


def compute_rolling_zscore_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day price z-score. Higher = more standard deviations above quarterly mean."""
    validate_prices(prices)
    wide = to_wide(prices)
    mean = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    std = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).std()
    z_raw = (wide - mean) / std.where(std > 0)
    z = cross_sectional_zscore(z_raw)
    result = to_long(z, "rolling_zscore_63d_score")
    logger.info("rolling_zscore_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

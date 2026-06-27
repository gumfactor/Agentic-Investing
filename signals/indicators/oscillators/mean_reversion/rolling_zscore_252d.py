"""252-day rolling Z-score factor.

Z = (Close - mean(252d)) / std(252d).
Measures how many standard deviations price is above its annual mean.
Positive = above annual average; higher = more extended vs own yearly history.
Dual-use: positive weight = secular trend (above long-run mean);
          negative weight = mean-reversion over a full annual cycle.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 252
_MIN_PERIODS = 126


def compute_rolling_zscore_252d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 252-day price z-score. Higher = more standard deviations above annual mean."""
    validate_prices(prices)
    wide = to_wide(prices)
    mean = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    std = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).std()
    z_raw = (wide - mean) / std.where(std > 0)
    z = cross_sectional_zscore(z_raw)
    result = to_long(z, "rolling_zscore_252d_score")
    logger.info("rolling_zscore_252d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

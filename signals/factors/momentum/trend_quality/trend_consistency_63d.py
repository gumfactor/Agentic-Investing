"""63-day trend consistency factor.

Fraction of days in the past 63 days with a positive return.
Higher = more consistent uptrend over the quarter = higher score.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

def compute_trend_consistency_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of fraction of positive-return days in the past 63 days."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    up_fraction = daily_ret.rolling(63, min_periods=44).apply(lambda x: (x > 0).mean(), raw=True)
    z = cross_sectional_zscore(up_fraction)
    result = to_long(z, "trend_consistency_63d_score")
    logger.info("trend_consistency_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

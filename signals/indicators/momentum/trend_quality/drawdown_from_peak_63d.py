"""63-day drawdown from peak factor.

Measures how far below its 63-day rolling high a stock is currently trading.
Score = price / rolling_63d_max (values 0-1, where 1.0 = at the peak).
Higher score = less drawdown = stronger recent price action.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

def compute_drawdown_from_peak_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / 63-day rolling high. Higher = less drawdown from recent peak."""
    validate_prices(prices)
    wide = to_wide(prices)
    rolling_peak = wide.rolling(63, min_periods=44).max()
    ratio = wide / rolling_peak
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "drawdown_from_peak_63d_score")
    logger.info("drawdown_from_peak_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

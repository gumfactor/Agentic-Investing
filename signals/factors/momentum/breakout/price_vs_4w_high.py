"""4-week high proximity factor.

Short-term breakout signal: stocks near their 4-week high score higher.
Score = price / rolling_20d_max.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

def compute_price_vs_4w_high_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / 20-day rolling high. Higher = nearer to 4-week high."""
    validate_prices(prices)
    wide = to_wide(prices)
    rolling_high = wide.rolling(20, min_periods=10).max()
    ratio = wide / rolling_high
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_4w_high_score")
    logger.info("price_vs_4w_high_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

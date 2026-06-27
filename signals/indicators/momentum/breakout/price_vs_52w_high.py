"""52-week high proximity factor.

Stocks trading near their 52-week high score higher (momentum signal).
Score = price / rolling_252d_max. Stocks at their high = 1.0; further below = lower.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

def compute_price_vs_52w_high_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / 252-day rolling high. Higher = nearer to 52-week high."""
    validate_prices(prices)
    wide = to_wide(prices)
    rolling_high = wide.rolling(252, min_periods=126).max()
    ratio = wide / rolling_high
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_52w_high_score")
    logger.info("price_vs_52w_high_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

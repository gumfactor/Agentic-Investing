"""4-week low proximity factor.

Stocks far above their 4-week low score higher (short-term strength signal).
Score = price / rolling_20d_min.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

def compute_price_vs_4w_low_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / 20-day rolling low. Higher = further above 4-week low."""
    validate_prices(prices)
    wide = to_wide(prices)
    rolling_low = wide.rolling(20, min_periods=10).min()
    ratio = wide / rolling_low
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_4w_low_score")
    logger.info("price_vs_4w_low_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

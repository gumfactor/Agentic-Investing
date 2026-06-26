"""Price vs 20-day SMA factor.

Score = price / SMA(20). Values above 1.0 mean price is above its 20-day average.
Short-term trend-following signal: stocks above their 20-day SMA score higher.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

def compute_price_vs_sma_20_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / SMA(20). Higher = further above 20-day average."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=20)
    ratio = wide / sma
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_sma_20_score")
    logger.info("price_vs_sma_20_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

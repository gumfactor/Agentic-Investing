"""Price vs 200-day SMA factor.

Score = price / SMA(200). Values above 1.0 mean price is above its 200-day average.
Long-term secular trend signal: stocks above their 200-day SMA are considered
in a structural uptrend. Classic institutional filter and trend confirmation signal.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

def compute_price_vs_sma_200_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / SMA(200). Higher = further above 200-day average."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=200)
    ratio = wide / sma
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_sma_200_score")
    logger.info("price_vs_sma_200_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

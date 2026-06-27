"""50/200-day SMA crossover factor (Golden Cross / Death Cross).

Score = SMA(50) / SMA(200). Values above 1.0 = fast MA above slow MA (Golden Cross).
Long-term secular trend signal; widely followed by institutional investors.
Positive divergence from 1.0 = degree of bullishness of the cross.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

def compute_ma_cross_50_200_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of SMA(50) / SMA(200). Higher = Golden Cross and further divergence."""
    validate_prices(prices)
    wide = to_wide(prices)
    fast = compute_sma(wide, window=50)
    slow = compute_sma(wide, window=200)
    ratio = fast / slow
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "ma_cross_50_200_score")
    logger.info("ma_cross_50_200_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

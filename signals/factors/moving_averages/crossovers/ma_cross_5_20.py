"""5/20-day SMA crossover factor.

Score = SMA(5) / SMA(20). Values above 1.0 = fast MA above slow MA (bullish cross).
Short-term trend signal; sensitive to recent price changes.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

def compute_ma_cross_5_20_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of SMA(5) / SMA(20). Higher = faster MA further above slower MA."""
    validate_prices(prices)
    wide = to_wide(prices)
    fast = compute_sma(wide, window=5)
    slow = compute_sma(wide, window=20)
    ratio = fast / slow
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "ma_cross_5_20_score")
    logger.info("ma_cross_5_20_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

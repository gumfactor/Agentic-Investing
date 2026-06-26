"""20/50-day SMA crossover factor.

Score = SMA(20) / SMA(50). Values above 1.0 = fast MA above slow MA (bullish).
Intermediate-term trend signal; the 20/50 cross is a common swing-trading trigger.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

def compute_ma_cross_20_50_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of SMA(20) / SMA(50). Higher = faster MA further above slower MA."""
    validate_prices(prices)
    wide = to_wide(prices)
    fast = compute_sma(wide, window=20)
    slow = compute_sma(wide, window=50)
    ratio = fast / slow
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "ma_cross_20_50_score")
    logger.info("ma_cross_20_50_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

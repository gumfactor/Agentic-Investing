"""50-day SMA slope factor.

Rate of change of the 50-day SMA, normalised by price: (SMA(50) - SMA(50).shift(21)) / price.
Positive = SMA is rising (uptrend accelerating); negative = SMA is falling.
Captures the direction and speed of the medium-term trend.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

_SLOPE_LAG = 21  # 1-month lag for slope measurement


def compute_ma_slope_50_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day rate of change of SMA(50), normalised by price."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=50)
    slope = (sma - sma.shift(_SLOPE_LAG)) / wide
    z = cross_sectional_zscore(slope)
    result = to_long(z, "ma_slope_50_score")
    logger.info("ma_slope_50_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

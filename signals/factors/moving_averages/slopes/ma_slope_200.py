"""200-day SMA slope factor.

Rate of change of the 200-day SMA, normalised by price: (SMA(200) - SMA(200).shift(63)) / price.
Uses a 63-day (quarter) lag because the 200-day MA moves slowly.
Positive = long-term trend is rising; negative = long-term trend is declining.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

_SLOPE_LAG = 63  # 1-quarter lag appropriate for the slow-moving 200-day MA


def compute_ma_slope_200_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day rate of change of SMA(200), normalised by price."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=200)
    slope = (sma - sma.shift(_SLOPE_LAG)) / wide
    z = cross_sectional_zscore(slope)
    result = to_long(z, "ma_slope_200_score")
    logger.info("ma_slope_200_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

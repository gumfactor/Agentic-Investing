"""MACD Histogram factor (12/26/9).

Histogram = MACD Line - Signal Line
         = (EMA(12) - EMA(26)) - EMA(9) of MACD Line.
Positive histogram = MACD line above signal = momentum accelerating upward.
Normalised by price so values are cross-sectionally comparable.
The histogram is more useful than the raw MACD line because it captures
the *change* in momentum, not just momentum itself.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_ema

logger = structlog.get_logger(__name__)


def compute_macd_histogram_12_26_9_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of MACD histogram / price. Positive = momentum accelerating."""
    validate_prices(prices)
    wide = to_wide(prices)
    ema_fast = compute_ema(wide, span=12)
    ema_slow = compute_ema(wide, span=26)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = (macd_line - signal_line) / wide
    z = cross_sectional_zscore(histogram)
    result = to_long(z, "macd_histogram_12_26_9_score")
    logger.info("macd_histogram_12_26_9_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

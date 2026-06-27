"""Bollinger Band Z-score factor (20-day).

Z = (Close - SMA(20)) / StdDev(20).
Measures how many standard deviations price is above or below its 20-day mean.
Positive = above mean; higher = more extended above mean.
Equivalent to the Bollinger %B midpoint without the band scaling.
Can serve as a momentum signal (higher = stronger) or a mean-reversion
signal (use with a negative strategy weight when price is overextended).
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

_WINDOW = 20
_MIN_PERIODS = 14


def compute_bb_z_score_20_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of (price - SMA20) / std20. Higher = more standard deviations above mean."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=_WINDOW)
    std = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).std()
    z_raw = (wide - sma) / std.where(std > 0)
    z = cross_sectional_zscore(z_raw)
    result = to_long(z, "bb_z_score_20_score")
    logger.info("bb_z_score_20_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

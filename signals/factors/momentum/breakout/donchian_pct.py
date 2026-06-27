"""Donchian channel position factor (63-day default window).

Measures where price sits within its N-day high/low range.
0.0 = at the N-day low, 1.0 = at the N-day high.
Stocks near the top of their range score higher (breakout/momentum signal).
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 63


def compute_donchian_pct_scores(prices: pd.DataFrame, window: int = _WINDOW) -> pd.DataFrame:
    """Cross-sectional z-score of position within N-day Donchian channel.
    Higher = price nearer the top of its recent range."""
    validate_prices(prices)
    wide = to_wide(prices)
    min_periods = int(window * 0.7)
    high = wide.rolling(window, min_periods=min_periods).max()
    low = wide.rolling(window, min_periods=min_periods).min()
    channel_width = high - low
    # Avoid division by zero for flat price series
    pct = (wide - low) / channel_width.where(channel_width > 0)
    z = cross_sectional_zscore(pct)
    result = to_long(z, "donchian_pct_score")
    logger.info("donchian_pct_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

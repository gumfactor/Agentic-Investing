"""MACD Signal Line factor (12/26/9).

Signal Line = EMA(9) of (EMA(12) - EMA(26)).
Smoothed version of the MACD line; used as a standalone trend bias signal.
Positive = smoothed MACD above zero = medium-term bullish trend.
Normalised by price for cross-sectional comparability.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_ema

logger = structlog.get_logger(__name__)


def compute_macd_signal_line_12_26_9_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of MACD signal line / price. Higher = smoothed uptrend bias."""
    validate_prices(prices)
    wide = to_wide(prices)
    ema_fast = compute_ema(wide, span=12)
    ema_slow = compute_ema(wide, span=26)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=9, adjust=False).mean() / wide
    z = cross_sectional_zscore(signal_line)
    result = to_long(z, "macd_signal_line_12_26_9_score")
    logger.info("macd_signal_line_12_26_9_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

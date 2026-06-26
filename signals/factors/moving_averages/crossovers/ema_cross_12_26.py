"""12/26-day EMA crossover factor (MACD line).

Score = EMA(12) - EMA(26), normalised by price level (divided by price).
This is the MACD line expressed as a fraction of price so it's cross-sectionally
comparable. Positive = EMA(12) above EMA(26) = short-term bullish momentum.

Note: for the full MACD oscillator with signal line and histogram, see the
oscillators/macd.py factor. This file isolates just the raw MACD line.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_ema

logger = structlog.get_logger(__name__)

def compute_ema_cross_12_26_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of (EMA(12) - EMA(26)) / price. Positive = MACD line bullish."""
    validate_prices(prices)
    wide = to_wide(prices)
    fast = compute_ema(wide, span=12)
    slow = compute_ema(wide, span=26)
    macd_line = (fast - slow) / wide
    z = cross_sectional_zscore(macd_line)
    result = to_long(z, "ema_cross_12_26_score")
    logger.info("ema_cross_12_26_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

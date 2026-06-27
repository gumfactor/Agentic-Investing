"""Price vs 26-day EMA factor.

Score = price / EMA(26). EMA is more responsive to recent price changes than SMA.
Short-to-medium term momentum signal used in MACD (slow component).
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_ema

logger = structlog.get_logger(__name__)

def compute_price_vs_ema_26_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / EMA(26). Higher = further above 26-day EMA."""
    validate_prices(prices)
    wide = to_wide(prices)
    ema = compute_ema(wide, span=26)
    ratio = wide / ema
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_ema_26_score")
    logger.info("price_vs_ema_26_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

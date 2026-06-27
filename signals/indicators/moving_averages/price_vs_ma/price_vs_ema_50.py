"""Price vs 50-day EMA factor.

Score = price / EMA(50). Intermediate-term exponential trend signal.
More weight on recent prices versus SMA(50), reacts faster to trend changes.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_ema

logger = structlog.get_logger(__name__)

def compute_price_vs_ema_50_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / EMA(50). Higher = further above 50-day EMA."""
    validate_prices(prices)
    wide = to_wide(prices)
    ema = compute_ema(wide, span=50)
    ratio = wide / ema
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_ema_50_score")
    logger.info("price_vs_ema_50_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""Price vs 200-day EMA factor.

Score = price / EMA(200). Long-term exponential trend signal.
More responsive to recent price action than SMA(200); widely tracked by
institutional traders as a secular trend filter.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_ema

logger = structlog.get_logger(__name__)

def compute_price_vs_ema_200_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / EMA(200). Higher = further above 200-day EMA."""
    validate_prices(prices)
    wide = to_wide(prices)
    ema = compute_ema(wide, span=200)
    ratio = wide / ema
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_ema_200_score")
    logger.info("price_vs_ema_200_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

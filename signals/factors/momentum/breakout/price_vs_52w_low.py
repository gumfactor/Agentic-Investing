"""52-week low proximity factor.

Stocks trading far above their 52-week low score higher (recovery/strength signal).
Score = price / rolling_252d_min. Stocks well above their low score higher.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

def compute_price_vs_52w_low_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of price / 252-day rolling low. Higher = further above 52-week low."""
    validate_prices(prices)
    wide = to_wide(prices)
    rolling_low = wide.rolling(252, min_periods=126).min()
    ratio = wide / rolling_low
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_52w_low_score")
    logger.info("price_vs_52w_low_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

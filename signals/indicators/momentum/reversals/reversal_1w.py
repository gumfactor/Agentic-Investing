"""1-week short-term reversal factor.

Stocks that fell over the past 5 days score higher — expected to mean-revert.
Sign convention: negated 5-day return, cross-sectionally z-scored.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)

def compute_reversal_1w_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of negated 5-day return. Higher = stronger mean-reversion candidate."""
    validate_prices(prices)
    wide = to_wide(prices)
    z = cross_sectional_zscore(-price_return(wide, lookback=5, skip=0))
    result = to_long(z, "reversal_1w_score")
    logger.info("reversal_1w_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

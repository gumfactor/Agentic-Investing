"""1-month medium-term reversal factor.

Stocks that underperformed over the past 21 days score higher.
Sign convention: negated 21-day return, cross-sectionally z-scored.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)

def compute_reversal_1m_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of negated 21-day return. Higher = stronger mean-reversion candidate."""
    validate_prices(prices)
    wide = to_wide(prices)
    z = cross_sectional_zscore(-price_return(wide, lookback=21, skip=0))
    result = to_long(z, "reversal_1m_score")
    logger.info("reversal_1m_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

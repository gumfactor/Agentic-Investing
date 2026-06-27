"""36-month long-term contrarian reversal factor (De Bondt & Thaler 1985).

Long-term losers tend to outperform over subsequent years.
Sign convention: negated 756-day return (skipping most recent 21 days), z-scored.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)

def compute_reversal_36m_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of negated 756-day return. Higher = stronger contrarian candidate."""
    validate_prices(prices)
    wide = to_wide(prices)
    z = cross_sectional_zscore(-price_return(wide, lookback=756, skip=21))
    result = to_long(z, "reversal_36m_score")
    logger.info("reversal_36m_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

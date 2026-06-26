"""1-month price momentum factor (21-day return, 21-day skip)."""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)

def compute_mom_1m_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day return skipping the most recent 21 days
    to avoid short-term reversal contamination (Jegadeesh & Titman 1993)."""
    validate_prices(prices)
    wide = to_wide(prices)
    z = cross_sectional_zscore(price_return(wide, lookback=21, skip=21))
    result = to_long(z, "mom_1m_score")
    logger.info("mom_1m_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""3-month price momentum factor (63-day return, 21-day skip)."""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)

def compute_mom_3m_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day return skipping the most recent 21 days."""
    validate_prices(prices)
    wide = to_wide(prices)
    z = cross_sectional_zscore(price_return(wide, lookback=63, skip=21))
    result = to_long(z, "mom_3m_score")
    logger.info("mom_3m_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

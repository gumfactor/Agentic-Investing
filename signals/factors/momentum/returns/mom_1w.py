"""1-week price momentum factor (5-day return, no skip)."""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)

def compute_mom_1w_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 5-day price return. Higher = stronger 1-week momentum."""
    validate_prices(prices)
    wide = to_wide(prices)
    z = cross_sectional_zscore(price_return(wide, lookback=5, skip=0))
    result = to_long(z, "mom_1w_score")
    logger.info("mom_1w_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

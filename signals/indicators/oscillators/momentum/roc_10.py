"""10-day Rate of Change (ROC) factor.

ROC(10) = (Close - Close[10]) / Close[10].
Raw short-term % return with no smoothing or skip; pure price velocity.
Higher = faster recent upward price movement.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_LOOKBACK = 10


def compute_roc_10_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 10-day Rate of Change. Higher = faster short-term momentum."""
    validate_prices(prices)
    wide = to_wide(prices)
    roc = wide / wide.shift(_LOOKBACK) - 1
    z = cross_sectional_zscore(roc)
    result = to_long(z, "roc_10_score")
    logger.info("roc_10_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

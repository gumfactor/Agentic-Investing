"""21-day Rate of Change (ROC) factor.

ROC(21) = (Close - Close[21]) / Close[21].
One-month raw % return with no skip; pure 1-month price velocity.
Higher = stronger 1-month upward price movement.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_LOOKBACK = 21


def compute_roc_21_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day Rate of Change. Higher = stronger 1-month momentum."""
    validate_prices(prices)
    wide = to_wide(prices)
    roc = wide / wide.shift(_LOOKBACK) - 1
    z = cross_sectional_zscore(roc)
    result = to_long(z, "roc_21_score")
    logger.info("roc_21_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

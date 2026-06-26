"""10-day realized volatility factor.

Annualized standard deviation of daily returns over 10 days.
Short-term noise-sensitive vol estimate.
Higher = more volatile recently.
Sign convention: use negative strategy weight for low-vol (lower vol = better long).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 10
_MIN_PERIODS = 7


def compute_realized_vol_10d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 10-day annualized realized volatility."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    vol = daily_ret.rolling(_WINDOW, min_periods=_MIN_PERIODS).std() * np.sqrt(252)
    z = cross_sectional_zscore(vol)
    result = to_long(z, "realized_vol_10d_score")
    logger.info("realized_vol_10d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

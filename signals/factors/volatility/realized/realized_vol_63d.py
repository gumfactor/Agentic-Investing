"""63-day realized volatility factor.

Annualized standard deviation of daily returns over 63 days (one quarter).
Smoother and less sensitive to short-term spikes than the 21-day estimate.
Higher = more volatile over the past quarter.
Sign convention: use negative strategy weight for low-vol (lower vol = better long).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 44


def compute_realized_vol_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day annualized realized volatility."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    vol = daily_ret.rolling(_WINDOW, min_periods=_MIN_PERIODS).std() * np.sqrt(252)
    z = cross_sectional_zscore(vol)
    result = to_long(z, "realized_vol_63d_score")
    logger.info("realized_vol_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

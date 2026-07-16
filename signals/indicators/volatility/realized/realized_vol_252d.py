"""252-day realized volatility factor.

Annualized standard deviation of daily returns over 252 days (one year).
Long-run risk baseline; slow-moving and stable.
Higher = structurally more volatile stock over the past year.
Sign convention: use negative strategy weight for low-vol (lower vol = better long).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return

logger = structlog.get_logger(__name__)

_WINDOW = 252
_MIN_PERIODS = 252  # full window (BUG-010)


def compute_realized_vol_252d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 252-day annualized realized volatility."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = daily_return(wide)
    vol = daily_ret.rolling(_WINDOW, min_periods=_MIN_PERIODS).std() * np.sqrt(252)
    z = cross_sectional_zscore(vol)
    result = to_long(z, "realized_vol_252d_score")
    logger.info("realized_vol_252d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

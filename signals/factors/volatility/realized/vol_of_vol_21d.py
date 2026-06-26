"""Volatility-of-volatility factor (21-day base, 63-day outer window).

Computes the 21-day rolling realized vol, then measures the standard deviation
of that series over the past 63 days. High VoV = vol is itself unstable,
indicating an uncertain or transitioning volatility regime.
Higher = more vol instability.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)


def compute_vol_of_vol_21d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of std(rolling_vol_21d) over 63 days. Higher = more vol instability."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    vol_21d = daily_ret.rolling(21, min_periods=15).std() * np.sqrt(252)
    vov = vol_21d.rolling(63, min_periods=44).std()
    z = cross_sectional_zscore(vov)
    result = to_long(z, "vol_of_vol_21d_score")
    logger.info("vol_of_vol_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

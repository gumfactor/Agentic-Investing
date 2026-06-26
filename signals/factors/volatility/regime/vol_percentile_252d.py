"""Vol percentile factor (252-day lookback).

Rank of current 21-day realized vol within its own 252-day history (0–1).
0.0 = current vol at its annual low; 1.0 = at its annual high.
Higher = currently more volatile than usual for this stock.
Useful as a regime filter: combine with other factors to avoid entering
positions when a stock's vol is at a multi-year high.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)


def compute_vol_percentile_252d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of percentile rank of current 21d vol in its 252d history."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    vol_21 = daily_ret.rolling(21, min_periods=15).std() * np.sqrt(252)
    pct_rank = vol_21.rolling(252, min_periods=126).rank(pct=True)
    z = cross_sectional_zscore(pct_rank)
    result = to_long(z, "vol_percentile_252d_score")
    logger.info("vol_percentile_252d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

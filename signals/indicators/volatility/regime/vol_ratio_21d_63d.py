"""Short/medium vol ratio factor.

vol_21d / vol_63d. Values above 1.0 = near-term vol is elevated vs its medium-term
baseline, indicating a volatility spike or regime shift.
Values below 1.0 = vol is calm relative to recent history.
Higher = near-term vol expansion.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return

logger = structlog.get_logger(__name__)


def compute_vol_ratio_21d_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of realized_vol(21d) / realized_vol(63d)."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = daily_return(wide)
    # full window (BUG-010): each rolling std requires its full lookback of contiguous, non-gapped returns
    vol_21 = daily_ret.rolling(21, min_periods=21).std()
    vol_63 = daily_ret.rolling(63, min_periods=63).std()
    ratio = vol_21 / vol_63.where(vol_63 > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "vol_ratio_21d_63d_score")
    logger.info("vol_ratio_21d_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

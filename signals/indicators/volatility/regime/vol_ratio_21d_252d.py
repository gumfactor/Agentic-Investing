"""Short/annual vol ratio factor.

vol_21d / vol_252d. Compares current near-term volatility to the annual baseline.
Values above 1.0 = current vol elevated vs full-year baseline.
Values below 1.0 = currently calmer than annual norm.
Higher = more short-term stress relative to long-run average.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return

logger = structlog.get_logger(__name__)


def compute_vol_ratio_21d_252d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of realized_vol(21d) / realized_vol(252d)."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = daily_return(wide)
    # full window (BUG-010): each rolling std requires its full lookback of contiguous, non-gapped returns
    vol_21 = daily_ret.rolling(21, min_periods=21).std()
    vol_252 = daily_ret.rolling(252, min_periods=252).std()
    ratio = vol_21 / vol_252.where(vol_252 > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "vol_ratio_21d_252d_score")
    logger.info("vol_ratio_21d_252d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

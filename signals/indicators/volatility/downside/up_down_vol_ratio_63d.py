"""Up/down volatility ratio factor (63-day).

upside_deviation(63d) / downside_deviation(63d).
Values above 1.0 = gains are larger than losses on average (positive skew).
Values below 1.0 = losses dominate (negative skew).
Higher = more asymmetric upside = better long candidate.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 63  # full window (BUG-010)


def compute_up_down_vol_ratio_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of upside_dev / downside_dev over 63 days. Higher = more positive asymmetry."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = daily_return(wide)
    pos_ret = daily_ret.clip(lower=0)
    neg_ret = daily_ret.clip(upper=0)
    upside = (pos_ret ** 2).rolling(_WINDOW, min_periods=_MIN_PERIODS).mean().pow(0.5)
    downside = (neg_ret ** 2).rolling(_WINDOW, min_periods=_MIN_PERIODS).mean().pow(0.5)
    ratio = upside / downside.where(downside > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "up_down_vol_ratio_63d_score")
    logger.info("up_down_vol_ratio_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

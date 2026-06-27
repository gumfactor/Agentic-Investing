"""63-day Sharpe ratio factor.

Annualized mean return / annualized realized volatility over 63 days.
Reward per unit of total risk over the past quarter.
Higher = better risk-adjusted return.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 44


def compute_sharpe_ratio_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day Sharpe ratio. Higher = better risk-adjusted return."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    ann_ret = daily_ret.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean() * 252
    ann_vol = daily_ret.rolling(_WINDOW, min_periods=_MIN_PERIODS).std() * np.sqrt(252)
    sharpe = ann_ret / ann_vol.where(ann_vol > 0)
    z = cross_sectional_zscore(sharpe)
    result = to_long(z, "sharpe_ratio_63d_score")
    logger.info("sharpe_ratio_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

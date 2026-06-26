"""252-day Sharpe ratio factor.

Annualized mean return / annualized realized volatility over 252 days.
Full-year risk-adjusted return; less noisy than the 63-day version.
Higher = better long-run risk-adjusted return.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 252
_MIN_PERIODS = 126


def compute_sharpe_ratio_252d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 252-day Sharpe ratio. Higher = better annual risk-adjusted return."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    ann_ret = daily_ret.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean() * 252
    ann_vol = daily_ret.rolling(_WINDOW, min_periods=_MIN_PERIODS).std() * np.sqrt(252)
    sharpe = ann_ret / ann_vol.where(ann_vol > 0)
    z = cross_sectional_zscore(sharpe)
    result = to_long(z, "sharpe_ratio_252d_score")
    logger.info("sharpe_ratio_252d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

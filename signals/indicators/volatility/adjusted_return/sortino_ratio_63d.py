"""63-day Sortino ratio factor.

Annualized mean return / annualized downside deviation over 63 days.
Like Sharpe but penalises only harmful volatility (downside). Preferred over
Sharpe when return distributions are asymmetric.
Higher = better downside-risk-adjusted return.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 63  # full window (BUG-010)


def compute_sortino_ratio_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day Sortino ratio. Higher = better downside-adjusted return."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = daily_return(wide)
    ann_ret = daily_ret.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean() * 252
    neg_ret = daily_ret.clip(upper=0)
    downside = (neg_ret ** 2).rolling(_WINDOW, min_periods=_MIN_PERIODS).mean().pow(0.5) * np.sqrt(252)
    sortino = ann_ret / downside.where(downside > 0)
    z = cross_sectional_zscore(sortino)
    result = to_long(z, "sortino_ratio_63d_score")
    logger.info("sortino_ratio_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

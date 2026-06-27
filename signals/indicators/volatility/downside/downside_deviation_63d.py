"""63-day downside deviation factor.

Semi-deviation: RMS of daily returns that are below zero, annualized.
Captures only the harmful volatility (losses), ignoring upside swings.
Used in the Sortino ratio denominator.
Higher = more downside risk.
Sign convention: use negative strategy weight to prefer low-downside stocks.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 44


def compute_downside_deviation_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day annualized downside deviation."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    neg_ret = daily_ret.clip(upper=0)
    downside = (neg_ret ** 2).rolling(_WINDOW, min_periods=_MIN_PERIODS).mean().pow(0.5) * np.sqrt(252)
    z = cross_sectional_zscore(downside)
    result = to_long(z, "downside_deviation_63d_score")
    logger.info("downside_deviation_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""63-day maximum drawdown factor.

Worst peak-to-trough loss within the 63-day rolling window.
Computed as min over the window of (price / cumulative_max - 1).
Negated so that higher score = smaller max drawdown = better long candidate.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 44


def _max_drawdown(x: np.ndarray) -> float:
    mask = ~np.isnan(x)
    if mask.sum() < 20:
        return np.nan
    p = x[mask]
    peak = np.maximum.accumulate(p)
    dd = p / peak - 1
    return float(np.min(dd))


def compute_max_drawdown_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of negated 63-day max drawdown. Higher = smaller worst loss."""
    validate_prices(prices)
    wide = to_wide(prices)
    max_dd = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).apply(_max_drawdown, raw=True)
    # max_dd values are <= 0; negate so higher score = less drawdown = better
    z = cross_sectional_zscore(-max_dd)
    result = to_long(z, "max_drawdown_63d_score")
    logger.info("max_drawdown_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

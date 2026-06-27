"""63-day Calmar ratio factor.

Total return over 63 days / absolute value of worst peak-to-trough drawdown
in the same 63-day window.
Higher = better return relative to the worst loss experienced.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 44


def _max_drawdown(x: np.ndarray) -> float:
    mask = ~np.isnan(x)
    if mask.sum() < 20:
        return np.nan
    p = x[mask]
    peak = np.maximum.accumulate(p)
    return float(np.min(p / peak - 1))


def compute_calmar_ratio_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day Calmar ratio. Higher = better return per unit of max drawdown."""
    validate_prices(prices)
    wide = to_wide(prices)
    ret_63d = price_return(wide, lookback=_WINDOW, skip=0)
    max_dd = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).apply(_max_drawdown, raw=True)
    abs_dd = max_dd.abs().where(max_dd < 0)
    calmar = ret_63d / abs_dd.where(abs_dd > 0)
    z = cross_sectional_zscore(calmar)
    result = to_long(z, "calmar_ratio_63d_score")
    logger.info("calmar_ratio_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""14-day Stochastic %D factor.

%D = 3-day SMA of %K(14). The smoothed signal line of the Stochastic oscillator.
Less noisy than %K; crossovers of %K and %D are common entry signals.
Higher = smoothed momentum near top of recent range.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 14
_MIN_PERIODS = 10
_SMOOTH = 3


def compute_stoch_d_14_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of Stochastic %D(14): 3-day SMA of %K(14)."""
    validate_prices(prices)
    wide = to_wide(prices)
    roll_min = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).min()
    roll_max = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).max()
    channel = roll_max - roll_min
    pct_k = (wide - roll_min) / channel.where(channel > 0)
    pct_d = pct_k.rolling(_SMOOTH, min_periods=2).mean()
    z = cross_sectional_zscore(pct_d)
    result = to_long(z, "stoch_d_14_score")
    logger.info("stoch_d_14_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

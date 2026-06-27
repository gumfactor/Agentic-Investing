"""14-day Stochastic %K factor.

%K = (Close - Lowest(14)) / (Highest(14) - Lowest(14)).
0 = at 14-day low, 1 = at 14-day high. Approximated with close prices only
(no separate high/low OHLC bars).
Higher = price near top of its recent range = short-term momentum.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 14
_MIN_PERIODS = 10


def compute_stoch_k_14_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of Stochastic %K(14). Higher = price near 14-day high."""
    validate_prices(prices)
    wide = to_wide(prices)
    roll_min = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).min()
    roll_max = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).max()
    channel = roll_max - roll_min
    pct_k = (wide - roll_min) / channel.where(channel > 0)
    z = cross_sectional_zscore(pct_k)
    result = to_long(z, "stoch_k_14_score")
    logger.info("stoch_k_14_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

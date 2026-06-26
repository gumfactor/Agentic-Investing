"""14-day Williams %R factor.

%R = (Highest(14) - Close) / (Highest(14) - Lowest(14)).
Raw %R is 0 when at the 14-day high (overbought) and 1 when at the low (oversold).
Negated here so higher score = price nearer its 14-day high = bullish momentum.
Approximated with close prices only (no separate OHLC bars).
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 14
_MIN_PERIODS = 10


def compute_williams_r_14_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of negated Williams %R(14). Higher = price near 14-day high."""
    validate_prices(prices)
    wide = to_wide(prices)
    roll_min = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).min()
    roll_max = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).max()
    channel = roll_max - roll_min
    raw_r = (roll_max - wide) / channel.where(channel > 0)
    # Negate: raw_r = 0 at high (bullish), 1 at low (bearish) → -raw_r = higher is better
    z = cross_sectional_zscore(-raw_r)
    result = to_long(z, "williams_r_14_score")
    logger.info("williams_r_14_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""63-day upside deviation factor.

Semi-deviation: RMS of daily returns that are above zero, annualized.
Captures only the beneficial volatility (gains), ignoring downside swings.
Higher = more upside capture capacity.
Naturally positive sign: higher upside deviation = better for long candidates.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 63  # full window (BUG-010)


def compute_upside_deviation_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day annualized upside deviation."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = daily_return(wide)
    pos_ret = daily_ret.clip(lower=0)
    upside = (pos_ret ** 2).rolling(_WINDOW, min_periods=_MIN_PERIODS).mean().pow(0.5) * np.sqrt(252)
    z = cross_sectional_zscore(upside)
    result = to_long(z, "upside_deviation_63d_score")
    logger.info("upside_deviation_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

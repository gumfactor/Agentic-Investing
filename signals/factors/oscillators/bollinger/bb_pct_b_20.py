"""Bollinger Band %B factor (20-day, 2-sigma).

%B = (Close - Lower Band) / (Upper Band - Lower Band).
0 = at lower band, 0.5 = at midline (SMA), 1 = at upper band, >1 = above upper band.
Higher = price nearer or above upper band = short-term strength / breakout signal.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

_WINDOW = 20
_MIN_PERIODS = 14
_N_STD = 2.0


def compute_bb_pct_b_20_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of Bollinger %B(20). Higher = price nearer upper band."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=_WINDOW)
    std = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).std()
    upper = sma + _N_STD * std
    lower = sma - _N_STD * std
    band_width = upper - lower
    pct_b = (wide - lower) / band_width.where(band_width > 0)
    z = cross_sectional_zscore(pct_b)
    result = to_long(z, "bb_pct_b_20_score")
    logger.info("bb_pct_b_20_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

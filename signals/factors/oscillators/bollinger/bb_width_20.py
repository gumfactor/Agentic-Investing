"""Bollinger Band Width factor (20-day, 2-sigma).

Width = (Upper Band - Lower Band) / SMA(20).
Measures volatility expansion: wider bands = higher recent volatility.
Useful as a regime filter (narrow bands precede breakouts).
Higher = more volatility expansion relative to trend level.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

_WINDOW = 20
_MIN_PERIODS = 14
_N_STD = 2.0


def compute_bb_width_20_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of Bollinger Band width(20). Higher = more volatility expansion."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=_WINDOW)
    std = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).std()
    width = (_N_STD * 2 * std) / sma.where(sma > 0)
    z = cross_sectional_zscore(width)
    result = to_long(z, "bb_width_20_score")
    logger.info("bb_width_20_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

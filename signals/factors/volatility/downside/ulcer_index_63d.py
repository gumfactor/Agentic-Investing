"""63-day Ulcer Index factor.

Ulcer Index = sqrt(mean(D²)) where D = (price / rolling_max - 1) × 100.
Measures both the depth and duration of drawdowns; more punishing than std.
Negated so that higher score = less drawdown pain = better long candidate.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 44


def compute_ulcer_index_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of negated Ulcer Index(63d). Higher = less sustained drawdown pain."""
    validate_prices(prices)
    wide = to_wide(prices)
    rolling_max = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).max()
    drawdown_pct = (wide / rolling_max - 1) * 100
    ulcer = (drawdown_pct ** 2).rolling(_WINDOW, min_periods=_MIN_PERIODS).mean().pow(0.5)
    z = cross_sectional_zscore(-ulcer)
    result = to_long(z, "ulcer_index_63d_score")
    logger.info("ulcer_index_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

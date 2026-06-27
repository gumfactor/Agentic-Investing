"""Earnings consistency factor.

Fraction of last 8 quarters with positive EPS.
A simple count-based signal that rewards companies that sustain profitability
across different macro and seasonal conditions. A company with 8/8 positive
quarters scores 1.0; a persistent loss-maker scores 0.0. Requires at least
4 valid quarterly observations.
Rolling computed on the sparse quarterly series before alignment.
Higher = more consistently profitable.

Requires fundamentals column: eps_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)

_WINDOW = 8
_MIN_OBS = 4


def compute_earnings_consistency_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of fraction of quarters with positive EPS. Higher = more consistent."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_ttm"})
    price_wide = to_wide(prices)
    eps_wide = fund_to_wide(fundamentals, "eps_ttm")
    consistency = eps_wide.rolling(_WINDOW, min_periods=_MIN_OBS).apply(
        lambda x: (x > 0).mean(), raw=True
    )
    consistency_daily = align_fundamentals(consistency, price_wide.index)
    z = cross_sectional_zscore(consistency_daily)
    result = to_long(z, "earnings_consistency_score")
    logger.info("earnings_consistency_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

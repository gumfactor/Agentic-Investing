"""Trailing twelve-month earnings yield factor.

EPS_TTM / Price — the inverse of the trailing P/E ratio.
Higher = cheaper on current earnings = better value candidate.
Negative values are valid (loss-making companies score lowest).

Requires fundamentals column: eps_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_earnings_yield_ttm_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of EPS_TTM / Price. Higher = cheaper on trailing earnings."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_ttm"})
    price_wide = to_wide(prices)
    eps = align_fundamentals(fund_to_wide(fundamentals, "eps_ttm"), price_wide.index)
    yield_ = eps / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(yield_)
    result = to_long(z, "earnings_yield_ttm_score")
    logger.info("earnings_yield_ttm_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

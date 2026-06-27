"""Normalized (cyclically adjusted) earnings yield factor.

eps_normalized / Price, where eps_normalized is a long-run average EPS
(e.g. 10-year rolling average, inflation-adjusted) computed upstream
in the data pipeline and stored in the fundamentals table.
Equivalent to the inverse of the CAPE / Shiller P/E at the stock level.
More stable than trailing EPS; filters out cyclical peaks and troughs.
Higher = cheap relative to long-run earnings power.

Requires fundamentals column: eps_normalized
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_earnings_yield_normalized_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of eps_normalized / Price. Higher = cheaper on cycle-adjusted earnings."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_normalized"})
    price_wide = to_wide(prices)
    eps = align_fundamentals(fund_to_wide(fundamentals, "eps_normalized"), price_wide.index)
    yield_ = eps / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(yield_)
    result = to_long(z, "earnings_yield_normalized_score")
    logger.info("earnings_yield_normalized_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

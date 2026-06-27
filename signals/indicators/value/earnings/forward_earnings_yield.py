"""Forward earnings yield factor.

eps_forward / Price — the inverse of the forward P/E ratio.
Uses consensus analyst EPS estimates for the next twelve months.
More forward-looking than the TTM version; reflects market expectations
about future earnings rather than what has already been reported.
Higher = cheap on expected future earnings.

Requires fundamentals column: eps_forward
(populated from analyst estimate data, e.g. via a data vendor)
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_forward_earnings_yield_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of eps_forward / Price. Higher = cheaper on forward earnings."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_forward"})
    price_wide = to_wide(prices)
    eps = align_fundamentals(fund_to_wide(fundamentals, "eps_forward"), price_wide.index)
    yield_ = eps / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(yield_)
    result = to_long(z, "forward_earnings_yield_score")
    logger.info("forward_earnings_yield_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

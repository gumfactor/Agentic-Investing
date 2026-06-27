"""Log revenue factor (negated).

−ln(Revenue_TTM).
Operating scale proxy. Revenue-based size is useful when two companies have
the same market cap but very different revenue bases — a low-revenue, high-
multiple growth firm and a high-revenue, low-multiple mature firm are clearly
different sizes in economic terms. Revenue is always positive for operating
businesses, so log is always defined.
Higher = smaller revenue base = stronger small-company tilt.

Requires fundamentals column: revenue_ttm
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)


def compute_log_revenue_ttm_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −ln(Revenue_TTM). Higher = smaller operating scale."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm"})
    price_wide = to_wide(prices)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    log_rev = np.log(revenue.where(revenue > 0))
    z = cross_sectional_zscore(-log_rev)
    result = to_long(z, "log_revenue_ttm_score")
    logger.info("log_revenue_ttm_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

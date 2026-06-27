"""Revenue 3-year CAGR factor.

(Revenue_TTM / Revenue_TTM_lag3Y)^(1/3) − 1.
Revenue is almost always positive, so the base issue that affects EPS CAGR
rarely applies here. The 3-year window smooths seasonal and project-timing
noise while remaining actionable as a medium-term growth signal.
Higher = stronger compound top-line growth over three years.

Requires fundamentals column: revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_3Y = 756


def compute_revenue_growth_3y_cagr_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 3Y revenue CAGR. Higher = stronger compound top-line growth."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm"})
    price_wide = to_wide(prices)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    lag = revenue.shift(_LAG_3Y)
    ratio = revenue / lag.where(lag > 0)
    cagr = ratio.where(ratio > 0) ** (1 / 3) - 1
    z = cross_sectional_zscore(cagr)
    result = to_long(z, "revenue_growth_3y_cagr_score")
    logger.info("revenue_growth_3y_cagr_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

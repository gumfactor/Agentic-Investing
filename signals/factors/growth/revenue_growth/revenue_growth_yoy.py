"""Revenue year-over-year growth factor.

(Revenue_TTM − Revenue_TTM_lag1Y) / Revenue_TTM_lag1Y.
Top-line growth is harder to manipulate than earnings and doesn't require a
positive base to be meaningful (revenue is almost always positive).
Higher = faster top-line growth over the past year.

Requires fundamentals column: revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_revenue_growth_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY revenue growth. Higher = faster top-line expansion."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm"})
    price_wide = to_wide(prices)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    lag = revenue.shift(_LAG_1Y)
    growth = (revenue - lag) / lag.where(lag > 0)
    z = cross_sectional_zscore(growth)
    result = to_long(z, "revenue_growth_yoy_score")
    logger.info("revenue_growth_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""Net profit margin factor.

Net_Income_TTM / Revenue_TTM.
The bottom-line margin after all costs including interest and tax. More
volatile than gross or operating margin but reflects the full income
statement. Useful for financials and capital-light businesses where interest
income is part of operations.
Higher = more profit per dollar of revenue.

Requires fundamentals columns: net_income_ttm, revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_net_margin_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Net_Income_TTM / Revenue_TTM. Higher = stronger bottom-line."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_income_ttm", "revenue_ttm"})
    price_wide = to_wide(prices)
    net_income = align_fundamentals(fund_to_wide(fundamentals, "net_income_ttm"), price_wide.index)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    ratio = net_income / revenue.where(revenue > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "net_margin_score")
    logger.info("net_margin_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

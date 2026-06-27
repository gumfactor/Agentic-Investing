"""Operating profit margin factor.

EBIT_TTM / Revenue_TTM.
Captures the margin after operating costs (COGS + SG&A + D&A) but before
interest and tax — making it capital-structure-neutral and comparable across
companies with different leverage profiles.
Higher = more operating income per dollar of revenue.

Requires fundamentals columns: ebit_ttm, revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_operating_margin_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of EBIT_TTM / Revenue_TTM. Higher = better operating efficiency."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"ebit_ttm", "revenue_ttm"})
    price_wide = to_wide(prices)
    ebit = align_fundamentals(fund_to_wide(fundamentals, "ebit_ttm"), price_wide.index)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    ratio = ebit / revenue.where(revenue > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "operating_margin_score")
    logger.info("operating_margin_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

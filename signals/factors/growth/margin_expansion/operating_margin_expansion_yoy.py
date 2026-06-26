"""Operating margin expansion year-over-year factor.

Operating_Margin_now − Operating_Margin_lag1Y.
Captures whether the company is converting a growing share of revenue into
operating income after all operating costs (COGS, SG&A, D&A). Expansion
here reflects operational leverage, cost discipline, or scale benefits.
Expressed as a difference so negative values clearly signal contraction.
Higher = expanding operating margin = growing operating leverage.

Requires fundamentals columns: ebit_ttm, revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_operating_margin_expansion_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY operating margin change. Higher = stronger margin improvement."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"ebit_ttm", "revenue_ttm"})
    price_wide = to_wide(prices)
    ebit = align_fundamentals(fund_to_wide(fundamentals, "ebit_ttm"), price_wide.index)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    margin = ebit / revenue.where(revenue > 0)
    expansion = margin - margin.shift(_LAG_1Y)
    z = cross_sectional_zscore(expansion)
    result = to_long(z, "operating_margin_expansion_yoy_score")
    logger.info("operating_margin_expansion_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

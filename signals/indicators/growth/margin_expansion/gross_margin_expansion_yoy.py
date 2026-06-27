"""Gross margin expansion year-over-year factor.

Gross_Margin_now − Gross_Margin_lag1Y.
A positive value means the company is retaining more gross profit per
dollar of revenue than it did a year ago — indicating pricing power gains,
input cost improvements, or product mix shift toward higher-margin lines.
Expressed as a difference (not ratio) so it is naturally bounded.
Higher = expanding gross margin = improving pricing/cost position.

Requires fundamentals columns: gross_profit_ttm, revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_gross_margin_expansion_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY gross margin change. Higher = stronger margin expansion."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"gross_profit_ttm", "revenue_ttm"})
    price_wide = to_wide(prices)
    gross_profit = align_fundamentals(fund_to_wide(fundamentals, "gross_profit_ttm"), price_wide.index)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    margin = gross_profit / revenue.where(revenue > 0)
    expansion = margin - margin.shift(_LAG_1Y)
    z = cross_sectional_zscore(expansion)
    result = to_long(z, "gross_margin_expansion_yoy_score")
    logger.info("gross_margin_expansion_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

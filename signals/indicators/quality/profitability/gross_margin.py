"""Gross profit margin factor.

Gross_Profit_TTM / Revenue_TTM.
The most stable margin metric — unaffected by below-the-line items like
D&A policy, interest, or tax structuring. High gross margins indicate
durable pricing power or low input cost sensitivity.
Higher = more gross profit retained per dollar of revenue.

Requires fundamentals columns: gross_profit_ttm, revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_gross_margin_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Gross_Profit_TTM / Revenue_TTM. Higher = stronger pricing power."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"gross_profit_ttm", "revenue_ttm"})
    price_wide = to_wide(prices)
    gross_profit = align_fundamentals(fund_to_wide(fundamentals, "gross_profit_ttm"), price_wide.index)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    ratio = gross_profit / revenue.where(revenue > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "gross_margin_score")
    logger.info("gross_margin_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

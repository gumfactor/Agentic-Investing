"""EV/Sales inverse factor (Revenue-to-EV).

Revenue_TTM / Enterprise_Value — capital-structure-neutral sales yield.
Unlike sales-to-price, accounts for debt burden; a highly leveraged company
with a low P/S may not actually be cheap once debt is included.
Higher = more revenue per dollar of total enterprise value = cheaper.

Requires fundamentals columns: revenue_ttm, shares_outstanding, total_debt, cash
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals, compute_ev_wide,
)

logger = structlog.get_logger(__name__)

_EV_COLS = {"shares_outstanding", "total_debt", "cash"}


def compute_ev_to_sales_inverse_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Revenue_TTM / EV. Higher = more revenue per unit of enterprise value."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm"} | _EV_COLS)
    price_wide = to_wide(prices)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    ev = compute_ev_wide(price_wide, fundamentals)
    ratio = revenue / ev.where(ev > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "ev_to_sales_inverse_score")
    logger.info("ev_to_sales_inverse_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

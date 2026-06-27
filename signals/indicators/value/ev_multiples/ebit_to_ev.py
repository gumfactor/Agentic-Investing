"""EBIT-to-EV factor (inverse EV/EBIT).

EBIT_TTM / Enterprise_Value — capital-structure-neutral operating profitability.
Unlike EBITDA, EBIT includes depreciation/amortization as a genuine economic
cost for asset-intensive businesses. Often preferred for industrials and telecoms
where D&A proxies for maintenance capex.
Higher = more operating income per dollar of enterprise value = cheaper.

Requires fundamentals columns: ebit_ttm, shares_outstanding, total_debt, cash
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals, compute_ev_wide,
)

logger = structlog.get_logger(__name__)

_EV_COLS = {"shares_outstanding", "total_debt", "cash"}


def compute_ebit_to_ev_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of EBIT_TTM / EV. Higher = more operating income per unit of EV."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"ebit_ttm"} | _EV_COLS)
    price_wide = to_wide(prices)
    ebit = align_fundamentals(fund_to_wide(fundamentals, "ebit_ttm"), price_wide.index)
    ev = compute_ev_wide(price_wide, fundamentals)
    ratio = ebit / ev.where(ev > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "ebit_to_ev_score")
    logger.info("ebit_to_ev_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

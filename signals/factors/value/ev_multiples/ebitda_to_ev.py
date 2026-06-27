"""EBITDA-to-EV factor (inverse EV/EBITDA).

EBITDA_TTM / Enterprise_Value — capital-structure-neutral earnings quality.
EBITDA strips out depreciation/amortization and interest, making it useful
for comparing companies with different capital structures or D&A policies.
Higher = more operating earnings per dollar of enterprise value = cheaper.

Requires fundamentals columns: ebitda_ttm, shares_outstanding, total_debt, cash
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


def compute_ebitda_to_ev_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of EBITDA_TTM / EV. Higher = more operating earnings per unit of EV."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"ebitda_ttm"} | _EV_COLS)
    price_wide = to_wide(prices)
    ebitda = align_fundamentals(fund_to_wide(fundamentals, "ebitda_ttm"), price_wide.index)
    ev = compute_ev_wide(price_wide, fundamentals)
    ratio = ebitda / ev.where(ev > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "ebitda_to_ev_score")
    logger.info("ebitda_to_ev_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

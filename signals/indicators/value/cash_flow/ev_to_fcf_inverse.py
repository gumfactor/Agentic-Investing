"""EV/FCF inverse factor (FCF yield on enterprise value).

FCF_TTM / Enterprise_Value — capital-structure-neutral free cash flow yield.
Preferred over FCF/Price for companies with significant debt, where the
price alone understates the total cost of ownership.
Higher = more free cash flow per dollar of enterprise value.

Requires fundamentals columns: fcf_ttm, shares_outstanding, total_debt, cash
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


def compute_ev_to_fcf_inverse_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of FCF_TTM / EV. Higher = more free cash per unit of enterprise value."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"fcf_ttm"} | _EV_COLS)
    price_wide = to_wide(prices)
    fcf = align_fundamentals(fund_to_wide(fundamentals, "fcf_ttm"), price_wide.index)
    ev = compute_ev_wide(price_wide, fundamentals)
    ratio = fcf / ev.where(ev > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "ev_to_fcf_inverse_score")
    logger.info("ev_to_fcf_inverse_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

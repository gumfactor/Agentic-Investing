"""Net debt-to-EBITDA factor (negated).

−(Total_Debt − Cash) / EBITDA_TTM.
Standard leverage metric used by credit analysts and lenders. Net debt
(total debt minus cash) normalised by operating earnings capacity.
Negative net debt (more cash than debt) yields a negative ratio → very
attractive when negated. Undefined for negative EBITDA companies.
Negated so that higher score = lower leverage = better balance sheet quality.

Requires fundamentals columns: total_debt, cash, ebitda_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_net_debt_to_ebitda_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −Net_Debt / EBITDA. Higher = lower financial leverage."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"total_debt", "cash", "ebitda_ttm"})
    price_wide = to_wide(prices)
    debt = align_fundamentals(fund_to_wide(fundamentals, "total_debt"), price_wide.index)
    cash = align_fundamentals(fund_to_wide(fundamentals, "cash"), price_wide.index)
    ebitda = align_fundamentals(fund_to_wide(fundamentals, "ebitda_ttm"), price_wide.index)
    net_debt = debt - cash
    ratio = net_debt / ebitda.where(ebitda > 0)
    z = cross_sectional_zscore(-ratio)
    result = to_long(z, "net_debt_to_ebitda_score")
    logger.info("net_debt_to_ebitda_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

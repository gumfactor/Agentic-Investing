"""Debt-to-equity ratio factor (negated).

−Total_Debt / Shareholders_Equity.
Classic financial leverage metric. High D/E amplifies both gains and losses,
reduces financial flexibility, and increases default risk. Excludes negative-
equity companies (their D/E would be negative, misleadingly appearing low).
Negated so that higher score = lower leverage = stronger balance sheet.

Requires fundamentals columns: total_debt, shareholders_equity
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_debt_to_equity_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −Total_Debt / Equity (positive equity only). Higher = less leveraged."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"total_debt", "shareholders_equity"})
    price_wide = to_wide(prices)
    debt = align_fundamentals(fund_to_wide(fundamentals, "total_debt"), price_wide.index)
    equity = align_fundamentals(fund_to_wide(fundamentals, "shareholders_equity"), price_wide.index)
    ratio = debt / equity.where(equity > 0)
    z = cross_sectional_zscore(-ratio)
    result = to_long(z, "debt_to_equity_score")
    logger.info("debt_to_equity_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

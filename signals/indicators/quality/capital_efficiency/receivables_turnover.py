"""Receivables turnover factor.

Revenue_TTM / Accounts_Receivable.
How quickly a company collects cash from customers. High turnover indicates
efficient collections and low credit risk in the customer base. Declining
turnover can signal channel stuffing or deteriorating customer quality.
Higher = faster cash collection = better receivables management.

Requires fundamentals columns: revenue_ttm, accounts_receivable
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_receivables_turnover_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Revenue_TTM / Accounts_Receivable. Higher = faster collections."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm", "accounts_receivable"})
    price_wide = to_wide(prices)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    ar = align_fundamentals(fund_to_wide(fundamentals, "accounts_receivable"), price_wide.index)
    ratio = revenue / ar.where(ar > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "receivables_turnover_score")
    logger.info("receivables_turnover_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

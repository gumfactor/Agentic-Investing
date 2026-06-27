"""Return on Equity factor.

Net_Income_TTM / Shareholders_Equity.
Measures how efficiently a company generates profit from equity capital.
Excludes negative-equity companies (distressed / heavily leveraged) to
avoid spurious sign-flips.
Higher = more profit per dollar of book equity.

Requires fundamentals columns: net_income_ttm, shareholders_equity
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_roe_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Net_Income_TTM / Shareholders_Equity. Higher = better profitability."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_income_ttm", "shareholders_equity"})
    price_wide = to_wide(prices)
    net_income = align_fundamentals(fund_to_wide(fundamentals, "net_income_ttm"), price_wide.index)
    equity = align_fundamentals(fund_to_wide(fundamentals, "shareholders_equity"), price_wide.index)
    ratio = net_income / equity.where(equity > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "roe_score")
    logger.info("roe_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

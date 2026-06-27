"""Return on Capital Employed factor.

EBIT_TTM / (Total_Assets − Current_Liabilities).
Capital Employed = Total Assets − Current Liabilities (short-term obligations
net out, leaving only the long-term capital base). Unlike ROIC, uses reported
EBIT rather than a tax-adjusted NOPAT, so it's simpler to compute but slightly
less pure. Useful cross-check alongside ROIC for industrials and utilities.
Higher = more operating income per dollar of long-term capital deployed.

Requires fundamentals columns: ebit_ttm, total_assets, current_liabilities
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_roce_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of EBIT_TTM / Capital_Employed. Higher = better long-term capital use."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"ebit_ttm", "total_assets", "current_liabilities"})
    price_wide = to_wide(prices)
    ebit = align_fundamentals(fund_to_wide(fundamentals, "ebit_ttm"), price_wide.index)
    assets = align_fundamentals(fund_to_wide(fundamentals, "total_assets"), price_wide.index)
    cur_liab = align_fundamentals(fund_to_wide(fundamentals, "current_liabilities"), price_wide.index)
    capital_employed = assets - cur_liab
    ratio = ebit / capital_employed.where(capital_employed > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "roce_score")
    logger.info("roce_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""Dividend yield factor.

Dividends_Per_Share_TTM / Price.
Captures the income return component of total shareholder return. High
dividend yield can signal value or distress — context matters, but
cross-sectionally it loads strongly on the value factor.
Higher = more dividend income per dollar invested.

Requires fundamentals column: dividends_per_share
  = TTM dividends paid per share (diluted)
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_dividend_yield_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Dividends_Per_Share / Price. Higher = more income per dollar."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"dividends_per_share"})
    price_wide = to_wide(prices)
    dps = align_fundamentals(fund_to_wide(fundamentals, "dividends_per_share"), price_wide.index)
    yield_ = dps / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(yield_)
    result = to_long(z, "dividend_yield_score")
    logger.info("dividend_yield_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

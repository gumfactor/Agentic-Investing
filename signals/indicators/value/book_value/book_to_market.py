"""Book-to-market factor.

Book_Value_Per_Share / Price — the inverse of the Price-to-Book ratio.
The classic Fama-French HML value factor.
Higher = cheaper on balance sheet assets = better value candidate.
Negative values are valid (companies with negative equity rank lowest).

Requires fundamentals column: book_value_per_share
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_book_to_market_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Book_Value_Per_Share / Price. Higher = cheaper on book value."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"book_value_per_share"})
    price_wide = to_wide(prices)
    bvps = align_fundamentals(fund_to_wide(fundamentals, "book_value_per_share"), price_wide.index)
    ratio = bvps / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "book_to_market_score")
    logger.info("book_to_market_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

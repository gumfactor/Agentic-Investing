"""Tangible book-to-price factor.

Tangible_Book_Value_Per_Share / Price.
Strips intangible assets and goodwill from book value — measures cheapness
relative to hard, realisable assets only. More conservative than book-to-market;
avoids inflating book value with acquisition goodwill.
Higher = cheaper on tangible assets.

Requires fundamentals column: tangible_book_value_per_share
  = (Total Equity − Intangible Assets − Goodwill) / Shares Outstanding
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_tangible_book_to_price_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Tangible_Book_Per_Share / Price. Higher = cheaper on hard assets."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"tangible_book_value_per_share"})
    price_wide = to_wide(prices)
    tbvps = align_fundamentals(fund_to_wide(fundamentals, "tangible_book_value_per_share"), price_wide.index)
    ratio = tbvps / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "tangible_book_to_price_score")
    logger.info("tangible_book_to_price_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

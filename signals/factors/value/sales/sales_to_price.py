"""Sales-to-price factor (inverse Price-to-Sales).

Revenue_Per_Share_TTM / Price.
Useful for companies with negative earnings where P/E is meaningless.
Revenue is harder to manipulate than earnings and doesn't go negative.
Higher = cheaper relative to top-line revenue.

Requires fundamentals column: revenue_per_share
  = TTM Revenue / Shares Outstanding (diluted)
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_sales_to_price_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Revenue_Per_Share / Price. Higher = cheaper on revenue."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_per_share"})
    price_wide = to_wide(prices)
    rps = align_fundamentals(fund_to_wide(fundamentals, "revenue_per_share"), price_wide.index)
    ratio = rps / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "sales_to_price_score")
    logger.info("sales_to_price_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

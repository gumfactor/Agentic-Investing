"""Book value per share year-over-year growth factor.

(BVPS − BVPS_lag1Y) / BVPS_lag1Y  where BVPS = Equity / Shares.
Equity accumulation signal: growing book value per share means the company
is retaining earnings and/or growing intrinsic value faster than it dilutes.
Shrinking BVPS signals either losses, buybacks in excess of earnings, or
special dividends. Only defined when the base-year equity is positive.
Higher = faster equity per share accumulation.

Requires fundamentals columns: shareholders_equity, shares_outstanding
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_book_value_per_share_growth_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY BVPS growth. Higher = faster equity per share accumulation."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"shareholders_equity", "shares_outstanding"})
    price_wide = to_wide(prices)
    equity = align_fundamentals(fund_to_wide(fundamentals, "shareholders_equity"), price_wide.index)
    shares = align_fundamentals(fund_to_wide(fundamentals, "shares_outstanding"), price_wide.index)
    bvps = equity / shares.where(shares > 0)
    lag = bvps.shift(_LAG_1Y)
    growth = (bvps - lag) / lag.where(lag > 0)
    z = cross_sectional_zscore(growth)
    result = to_long(z, "book_value_per_share_growth_yoy_score")
    logger.info("book_value_per_share_growth_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

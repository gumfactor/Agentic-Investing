"""Buyback yield factor.

Net_Buybacks_Per_Share_TTM / Price.
Captures the capital return component from share repurchases. Net buybacks
(gross repurchases minus new issuances) avoid penalising companies that use
stock-based compensation heavily. Combined with dividends it forms the full
shareholder yield picture.
Higher = more capital returned via repurchases per dollar invested.

Requires fundamentals column: net_buybacks_per_share
  = (Gross repurchases − Share issuances) / Diluted shares (TTM)
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_buyback_yield_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Net_Buybacks_Per_Share / Price. Higher = more buyback return."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_buybacks_per_share"})
    price_wide = to_wide(prices)
    nbs = align_fundamentals(fund_to_wide(fundamentals, "net_buybacks_per_share"), price_wide.index)
    yield_ = nbs / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(yield_)
    result = to_long(z, "buyback_yield_score")
    logger.info("buyback_yield_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

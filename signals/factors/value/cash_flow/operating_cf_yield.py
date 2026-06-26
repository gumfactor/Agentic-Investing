"""Operating cash flow yield factor.

Operating_CF_Per_Share_TTM / Price.
Operating cash flow before capex — less volatile than FCF for capex-heavy
industries (utilities, telecoms, industrials). A useful complement to FCF yield.
Higher = more operating cash per dollar of price.

Requires fundamentals column: operating_cf_per_share
  = Operating Cash Flow / Shares Outstanding (TTM)
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_operating_cf_yield_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Op_CF_Per_Share / Price. Higher = more operating cash per dollar."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"operating_cf_per_share"})
    price_wide = to_wide(prices)
    ocf = align_fundamentals(fund_to_wide(fundamentals, "operating_cf_per_share"), price_wide.index)
    yield_ = ocf / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(yield_)
    result = to_long(z, "operating_cf_yield_score")
    logger.info("operating_cf_yield_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

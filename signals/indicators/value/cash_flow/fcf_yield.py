"""Free cash flow yield factor.

FCF_Per_Share_TTM / Price — the most predictive single value metric in
academic research (Berk & Green, Fama & French). FCF is cash left after
capex and is harder to manipulate than reported earnings.
Higher = more free cash flow per dollar of price = better value candidate.

Requires fundamentals column: fcf_per_share
  = (Operating Cash Flow − Capex) / Shares Outstanding (TTM)
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_fcf_yield_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of FCF_Per_Share / Price. Higher = more free cash per dollar of price."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"fcf_per_share"})
    price_wide = to_wide(prices)
    fcf = align_fundamentals(fund_to_wide(fundamentals, "fcf_per_share"), price_wide.index)
    yield_ = fcf / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(yield_)
    result = to_long(z, "fcf_yield_score")
    logger.info("fcf_yield_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

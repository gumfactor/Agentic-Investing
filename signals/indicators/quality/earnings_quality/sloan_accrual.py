"""Sloan accrual ratio factor (negated).

−(Net_Income_TTM − Operating_CF_TTM) / Total_Assets.
Sloan (1996): earnings backed by cash flow are more persistent than earnings
backed by accruals. High accruals = aggressive accounting recognition; low
accruals = cash-backed, higher quality earnings.
Negated so that higher score = lower accruals = higher earnings quality.

Requires fundamentals columns: net_income_ttm, operating_cf_ttm, total_assets
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_sloan_accrual_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −(NI − OCF) / Assets. Higher = more cash-backed earnings."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_income_ttm", "operating_cf_ttm", "total_assets"})
    price_wide = to_wide(prices)
    net_income = align_fundamentals(fund_to_wide(fundamentals, "net_income_ttm"), price_wide.index)
    ocf = align_fundamentals(fund_to_wide(fundamentals, "operating_cf_ttm"), price_wide.index)
    assets = align_fundamentals(fund_to_wide(fundamentals, "total_assets"), price_wide.index)
    accrual = (net_income - ocf) / assets.where(assets > 0)
    z = cross_sectional_zscore(-accrual)
    result = to_long(z, "sloan_accrual_score")
    logger.info("sloan_accrual_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""Balance sheet accrual ratio factor (negated).

−ΔNet_Operating_Assets / Avg_Total_Assets (Richardson et al. 2005).
Measures the year-over-year growth in net operating assets scaled by
average assets. Rising NOA signals asset inflation via accrual accounting.
Lower accruals (shrinking or flat NOA growth) = higher quality.
Negated so that higher score = lower accrual growth = better earnings quality.

YoY change is approximated by a 252-trading-day shift on the daily-aligned series.

Requires fundamentals columns: net_operating_assets, total_assets
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)

_YEAR_LAG = 252


def compute_balance_sheet_accrual_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −ΔNOA / Avg_Assets. Higher = lower accrual growth = better quality."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_operating_assets", "total_assets"})
    price_wide = to_wide(prices)
    noa = align_fundamentals(fund_to_wide(fundamentals, "net_operating_assets"), price_wide.index)
    assets = align_fundamentals(fund_to_wide(fundamentals, "total_assets"), price_wide.index)
    noa_prior = noa.shift(_YEAR_LAG)
    assets_prior = assets.shift(_YEAR_LAG)
    avg_assets = (assets + assets_prior) / 2
    accrual = (noa - noa_prior) / avg_assets.where(avg_assets > 0)
    z = cross_sectional_zscore(-accrual)
    result = to_long(z, "balance_sheet_accrual_score")
    logger.info("balance_sheet_accrual_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

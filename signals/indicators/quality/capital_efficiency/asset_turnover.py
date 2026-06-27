"""Asset turnover factor.

Revenue_TTM / Total_Assets.
Measures how efficiently a company converts its asset base into revenue.
Highly sector-dependent (retailers turn assets fast; utilities slow), so
the cross-sectional z-score is most informative within-sector or in
combination with margin factors (DuPont decomposition of ROA).
Higher = more revenue generated per dollar of assets.

Requires fundamentals columns: revenue_ttm, total_assets
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_asset_turnover_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Revenue_TTM / Total_Assets. Higher = more efficient asset use."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm", "total_assets"})
    price_wide = to_wide(prices)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    assets = align_fundamentals(fund_to_wide(fundamentals, "total_assets"), price_wide.index)
    ratio = revenue / assets.where(assets > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "asset_turnover_score")
    logger.info("asset_turnover_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

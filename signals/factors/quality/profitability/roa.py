"""Return on Assets factor.

Net_Income_TTM / Total_Assets.
Capital-structure-neutral profitability: doesn't penalise firms that fund
operations primarily via equity. More stable cross-sectionally than ROE
because total assets doesn't go negative.
Higher = more profit per dollar of assets deployed.

Requires fundamentals columns: net_income_ttm, total_assets
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_roa_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Net_Income_TTM / Total_Assets. Higher = better asset productivity."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_income_ttm", "total_assets"})
    price_wide = to_wide(prices)
    net_income = align_fundamentals(fund_to_wide(fundamentals, "net_income_ttm"), price_wide.index)
    assets = align_fundamentals(fund_to_wide(fundamentals, "total_assets"), price_wide.index)
    ratio = net_income / assets.where(assets > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "roa_score")
    logger.info("roa_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

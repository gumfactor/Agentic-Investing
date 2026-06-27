"""Current ratio factor.

Current_Assets / Current_Liabilities.
Measures short-term liquidity: can the company cover near-term obligations
from near-term assets? A ratio above 1 is generally considered healthy;
below 1 signals potential liquidity stress.
Higher = stronger short-term financial position.

Requires fundamentals columns: current_assets, current_liabilities
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_current_ratio_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Current_Assets / Current_Liabilities. Higher = better liquidity."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"current_assets", "current_liabilities"})
    price_wide = to_wide(prices)
    cur_assets = align_fundamentals(fund_to_wide(fundamentals, "current_assets"), price_wide.index)
    cur_liab = align_fundamentals(fund_to_wide(fundamentals, "current_liabilities"), price_wide.index)
    ratio = cur_assets / cur_liab.where(cur_liab > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "current_ratio_score")
    logger.info("current_ratio_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

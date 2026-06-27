"""Inventory turnover factor.

COGS_TTM / Inventory.
How quickly a company sells through its inventory. High turnover signals
strong demand, efficient procurement, and low obsolescence risk. Companies
with no inventory (service businesses, financials) are excluded as NaN.
Higher = faster inventory conversion = better working capital management.

Requires fundamentals columns: cogs_ttm, inventory
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_inventory_turnover_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of COGS_TTM / Inventory. Higher = faster inventory cycle."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"cogs_ttm", "inventory"})
    price_wide = to_wide(prices)
    cogs = align_fundamentals(fund_to_wide(fundamentals, "cogs_ttm"), price_wide.index)
    inventory = align_fundamentals(fund_to_wide(fundamentals, "inventory"), price_wide.index)
    ratio = cogs / inventory.where(inventory > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "inventory_turnover_score")
    logger.info("inventory_turnover_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

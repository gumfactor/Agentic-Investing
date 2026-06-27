"""Quick ratio factor (acid test).

(Current_Assets − Inventory) / Current_Liabilities.
A stricter liquidity test than the current ratio: inventory is excluded
because it may not convert quickly to cash. Particularly relevant for
retailers, manufacturers, and distributors with large inventory positions.
Higher = stronger immediate liquidity.

Requires fundamentals columns: current_assets, inventory, current_liabilities
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_quick_ratio_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of (Current_Assets − Inventory) / Current_Liabilities."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"current_assets", "inventory", "current_liabilities"})
    price_wide = to_wide(prices)
    cur_assets = align_fundamentals(fund_to_wide(fundamentals, "current_assets"), price_wide.index)
    inventory = align_fundamentals(fund_to_wide(fundamentals, "inventory"), price_wide.index)
    cur_liab = align_fundamentals(fund_to_wide(fundamentals, "current_liabilities"), price_wide.index)
    ratio = (cur_assets - inventory) / cur_liab.where(cur_liab > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "quick_ratio_score")
    logger.info("quick_ratio_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""Net margin expansion year-over-year factor.

Net_Margin_now − Net_Margin_lag1Y.
Bottom-line margin change captures the full income statement trajectory
including interest and tax. While more volatile than gross or operating
margin changes (due to non-recurring items), it reflects the ultimate
direction of shareholder profitability.
Higher = expanding net margin = growing bottom-line profitability.

Requires fundamentals columns: net_income_ttm, revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_net_margin_expansion_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY net margin change. Higher = growing bottom-line profitability."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_income_ttm", "revenue_ttm"})
    price_wide = to_wide(prices)
    net_income = align_fundamentals(fund_to_wide(fundamentals, "net_income_ttm"), price_wide.index)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    margin = net_income / revenue.where(revenue > 0)
    expansion = margin - margin.shift(_LAG_1Y)
    z = cross_sectional_zscore(expansion)
    result = to_long(z, "net_margin_expansion_yoy_score")
    logger.info("net_margin_expansion_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

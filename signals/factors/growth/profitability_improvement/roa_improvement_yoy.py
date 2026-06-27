"""ROA improvement year-over-year factor.

ROA_now − ROA_lag1Y.
Measures whether asset productivity is trending up or down. A rising ROA
can indicate either faster earnings growth or more efficient asset deployment
(or both). As a difference rather than a ratio it is symmetric: improvement
and deterioration are equally penalised/rewarded.
Higher = improving return on assets = better asset productivity trend.

Requires fundamentals columns: net_income_ttm, total_assets
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_roa_improvement_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY ROA change. Higher = improving asset return trend."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_income_ttm", "total_assets"})
    price_wide = to_wide(prices)
    net_income = align_fundamentals(fund_to_wide(fundamentals, "net_income_ttm"), price_wide.index)
    assets = align_fundamentals(fund_to_wide(fundamentals, "total_assets"), price_wide.index)
    roa = net_income / assets.where(assets > 0)
    improvement = roa - roa.shift(_LAG_1Y)
    z = cross_sectional_zscore(improvement)
    result = to_long(z, "roa_improvement_yoy_score")
    logger.info("roa_improvement_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

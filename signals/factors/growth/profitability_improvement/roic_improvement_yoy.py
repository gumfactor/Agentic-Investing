"""ROIC improvement year-over-year factor.

ROIC_now − ROIC_lag1Y.
ROIC is the most economically meaningful return metric (it measures returns
on capital actually deployed in operations). A rising ROIC above the cost of
capital signals value-creating growth; a falling ROIC signals deterioration
even if absolute profits are growing.
Requires pre-computed nopat_ttm and invested_capital from the data pipeline.
Higher = improving capital efficiency trend.

Requires fundamentals columns: nopat_ttm, invested_capital
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_roic_improvement_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY ROIC change. Higher = improving capital efficiency trend."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"nopat_ttm", "invested_capital"})
    price_wide = to_wide(prices)
    nopat = align_fundamentals(fund_to_wide(fundamentals, "nopat_ttm"), price_wide.index)
    ic = align_fundamentals(fund_to_wide(fundamentals, "invested_capital"), price_wide.index)
    roic = nopat / ic.where(ic > 0)
    improvement = roic - roic.shift(_LAG_1Y)
    z = cross_sectional_zscore(improvement)
    result = to_long(z, "roic_improvement_yoy_score")
    logger.info("roic_improvement_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

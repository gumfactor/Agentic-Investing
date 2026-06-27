"""ROE improvement year-over-year factor.

ROE_now − ROE_lag1Y.
Captures the direction of equity return trend. Expanding ROE indicates
either earnings growing faster than equity (earnings leverage) or share
buybacks reducing the equity base. Restricted to positive-equity firms
at both time points to avoid sign-flipping distortions.
Higher = improving return on equity trend.

Requires fundamentals columns: net_income_ttm, shareholders_equity
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_roe_improvement_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY ROE change (positive-equity firms). Higher = improving ROE trend."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"net_income_ttm", "shareholders_equity"})
    price_wide = to_wide(prices)
    net_income = align_fundamentals(fund_to_wide(fundamentals, "net_income_ttm"), price_wide.index)
    equity = align_fundamentals(fund_to_wide(fundamentals, "shareholders_equity"), price_wide.index)
    roe = net_income / equity.where(equity > 0)
    improvement = roe - roe.shift(_LAG_1Y)
    z = cross_sectional_zscore(improvement)
    result = to_long(z, "roe_improvement_yoy_score")
    logger.info("roe_improvement_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

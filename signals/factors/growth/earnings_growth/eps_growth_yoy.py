"""EPS year-over-year growth factor.

(EPS_TTM − EPS_TTM_lag1Y) / EPS_TTM_lag1Y.
Only defined when the base-year EPS is positive; a negative base makes
the sign of the growth rate economically meaningless.
Higher = faster earnings-per-share growth over the past year.

Requires fundamentals column: eps_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_eps_growth_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY EPS growth. Higher = faster earnings growth."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_ttm"})
    price_wide = to_wide(prices)
    eps = align_fundamentals(fund_to_wide(fundamentals, "eps_ttm"), price_wide.index)
    lag = eps.shift(_LAG_1Y)
    growth = (eps - lag) / lag.where(lag > 0)
    z = cross_sectional_zscore(growth)
    result = to_long(z, "eps_growth_yoy_score")
    logger.info("eps_growth_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

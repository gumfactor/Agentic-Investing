"""EPS 3-year CAGR factor.

(EPS_TTM / EPS_TTM_lag3Y)^(1/3) − 1.
Smooths out single-year noise and volatile base-year effects. Only defined
when both current and 3-year-lag EPS are positive (negative base → undefined
CAGR; negative current → CAGR undefined for odd-root of negative ratio).
Higher = stronger compound earnings growth over three years.

Requires fundamentals column: eps_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_3Y = 756


def compute_eps_growth_3y_cagr_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 3Y EPS CAGR. Higher = stronger compounding earnings growth."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_ttm"})
    price_wide = to_wide(prices)
    eps = align_fundamentals(fund_to_wide(fundamentals, "eps_ttm"), price_wide.index)
    lag = eps.shift(_LAG_3Y)
    ratio = eps / lag.where(lag > 0)
    cagr = ratio.where(ratio > 0) ** (1 / 3) - 1
    z = cross_sectional_zscore(cagr)
    result = to_long(z, "eps_growth_3y_cagr_score")
    logger.info("eps_growth_3y_cagr_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

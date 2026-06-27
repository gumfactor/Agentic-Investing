"""EBITDA year-over-year growth factor.

(EBITDA_TTM − EBITDA_TTM_lag1Y) / EBITDA_TTM_lag1Y.
EBITDA growth is the primary growth metric in credit and private equity
analysis: it measures whether cash operating earnings are expanding before
the effects of capital structure and D&A policy. Only defined when the
base-year EBITDA is positive.
Higher = faster operating earnings growth.

Requires fundamentals column: ebitda_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_ebitda_growth_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY EBITDA growth. Higher = faster operating earnings expansion."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"ebitda_ttm"})
    price_wide = to_wide(prices)
    ebitda = align_fundamentals(fund_to_wide(fundamentals, "ebitda_ttm"), price_wide.index)
    lag = ebitda.shift(_LAG_1Y)
    growth = (ebitda - lag) / lag.where(lag > 0)
    z = cross_sectional_zscore(growth)
    result = to_long(z, "ebitda_growth_yoy_score")
    logger.info("ebitda_growth_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

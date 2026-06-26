"""Free cash flow year-over-year growth factor.

(FCF_TTM − FCF_TTM_lag1Y) / FCF_TTM_lag1Y.
FCF growth is the most direct measure of whether a business is converting
more of its revenue into owner cash. Only defined when base-year FCF is
positive; a negative base makes the growth rate sign ambiguous.
Higher = faster free cash flow growth over the past year.

Requires fundamentals column: fcf_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_fcf_growth_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY FCF growth. Higher = faster free cash flow expansion."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"fcf_ttm"})
    price_wide = to_wide(prices)
    fcf = align_fundamentals(fund_to_wide(fundamentals, "fcf_ttm"), price_wide.index)
    lag = fcf.shift(_LAG_1Y)
    growth = (fcf - lag) / lag.where(lag > 0)
    z = cross_sectional_zscore(growth)
    result = to_long(z, "fcf_growth_yoy_score")
    logger.info("fcf_growth_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

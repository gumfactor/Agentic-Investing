"""Operating cash flow year-over-year growth factor.

(OCF_TTM − OCF_TTM_lag1Y) / OCF_TTM_lag1Y.
Operating cash flow growth before capex — useful for capex-heavy industries
where FCF swings are driven by lumpy investment cycles rather than underlying
business deterioration. Only defined when base-year OCF is positive.
Higher = faster operating cash generation growth.

Requires fundamentals column: operating_cf_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_ocf_growth_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY operating CF growth. Higher = faster cash generation growth."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"operating_cf_ttm"})
    price_wide = to_wide(prices)
    ocf = align_fundamentals(fund_to_wide(fundamentals, "operating_cf_ttm"), price_wide.index)
    lag = ocf.shift(_LAG_1Y)
    growth = (ocf - lag) / lag.where(lag > 0)
    z = cross_sectional_zscore(growth)
    result = to_long(z, "ocf_growth_yoy_score")
    logger.info("ocf_growth_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

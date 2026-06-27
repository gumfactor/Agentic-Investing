"""Operating cash flow 3-year CAGR factor.

(OCF_TTM / OCF_TTM_lag3Y)^(1/3) − 1.
Complements FCF 3Y CAGR for capex-intensive sectors. OCF excludes the
volatile capex component, so its CAGR more cleanly reflects whether the
core business is generating increasing cash over a multi-year period.
Requires both current and 3-year-lag OCF to be positive.
Higher = stronger compound operating cash generation growth.

Requires fundamentals column: operating_cf_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_3Y = 756


def compute_ocf_growth_3y_cagr_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 3Y OCF CAGR. Higher = stronger compound operating CF growth."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"operating_cf_ttm"})
    price_wide = to_wide(prices)
    ocf = align_fundamentals(fund_to_wide(fundamentals, "operating_cf_ttm"), price_wide.index)
    lag = ocf.shift(_LAG_3Y)
    ratio = ocf / lag.where(lag > 0)
    cagr = ratio.where(ratio > 0) ** (1 / 3) - 1
    z = cross_sectional_zscore(cagr)
    result = to_long(z, "ocf_growth_3y_cagr_score")
    logger.info("ocf_growth_3y_cagr_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

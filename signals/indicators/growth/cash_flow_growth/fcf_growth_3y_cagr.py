"""Free cash flow 3-year CAGR factor.

(FCF_TTM / FCF_TTM_lag3Y)^(1/3) − 1.
Three-year FCF compounding smooths lumpy capex cycles and one-time working
capital swings. Requires both current and 3-year-lag FCF to be positive
(undefined CAGR for negative or zero base).
Higher = stronger compound free cash flow growth over three years.

Requires fundamentals column: fcf_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_3Y = 756


def compute_fcf_growth_3y_cagr_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 3Y FCF CAGR. Higher = stronger compound free cash flow growth."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"fcf_ttm"})
    price_wide = to_wide(prices)
    fcf = align_fundamentals(fund_to_wide(fundamentals, "fcf_ttm"), price_wide.index)
    lag = fcf.shift(_LAG_3Y)
    ratio = fcf / lag.where(lag > 0)
    cagr = ratio.where(ratio > 0) ** (1 / 3) - 1
    z = cross_sectional_zscore(cagr)
    result = to_long(z, "fcf_growth_3y_cagr_score")
    logger.info("fcf_growth_3y_cagr_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

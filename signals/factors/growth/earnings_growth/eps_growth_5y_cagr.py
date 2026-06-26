"""EPS 5-year CAGR factor.

(EPS_TTM / EPS_TTM_lag5Y)^(1/5) − 1.
The longest-horizon earnings growth signal — reflects structural rather than
cyclical growth. Requires a full 5-year price history (~1260 trading days),
so newer listings or recent IPOs will be excluded as NaN. Only defined when
both current and 5-year-lag EPS are positive.
Higher = stronger long-run compound earnings growth.

Requires fundamentals column: eps_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_5Y = 1260


def compute_eps_growth_5y_cagr_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 5Y EPS CAGR. Higher = stronger long-run earnings compounding."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_ttm"})
    price_wide = to_wide(prices)
    eps = align_fundamentals(fund_to_wide(fundamentals, "eps_ttm"), price_wide.index)
    lag = eps.shift(_LAG_5Y)
    ratio = eps / lag.where(lag > 0)
    cagr = ratio.where(ratio > 0) ** (1 / 5) - 1
    z = cross_sectional_zscore(cagr)
    result = to_long(z, "eps_growth_5y_cagr_score")
    logger.info("eps_growth_5y_cagr_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

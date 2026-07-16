"""63-day market beta factor.

OLS beta vs SPY over 63 days: cov(ret, SPY_ret) / var(SPY_ret).
Beta > 1 = amplifies market moves; Beta < 1 = defensive.
Higher = more market-sensitive (higher systematic risk).
Requires SPY to be present in the prices DataFrame.
Sign convention: use negative weight to prefer low-beta; positive for high-beta tilt.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return
from signals.indicators._market_utils import rolling_beta

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 63  # full window (BUG-010)
_BENCHMARK = "SPY"


def compute_beta_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day beta vs SPY. Higher = more market sensitivity."""
    validate_prices(prices)
    wide = to_wide(prices)
    if _BENCHMARK not in wide.columns:
        raise ValueError("beta_63d requires 'SPY' to be present in prices")
    daily_ret = daily_return(wide)
    beta = rolling_beta(daily_ret, _BENCHMARK, _WINDOW, _MIN_PERIODS)
    beta = beta.drop(columns=[_BENCHMARK], errors="ignore")
    z = cross_sectional_zscore(beta)
    result = to_long(z, "beta_63d_score")
    logger.info("beta_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

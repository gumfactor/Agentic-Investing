"""63-day beta stability factor.

Standard deviation of rolling 21-day betas over the past 63 days.
Measures how much a stock's market sensitivity shifts over time.
Negated so that higher score = more stable beta = more predictable market behaviour.
Requires SPY to be present in the prices DataFrame.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._market_utils import rolling_beta

logger = structlog.get_logger(__name__)

_SHORT_WINDOW = 21
_SHORT_MIN = 15
_OUTER_WINDOW = 63
_OUTER_MIN = 44
_BENCHMARK = "SPY"


def compute_beta_stability_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of negated std(rolling_21d_beta) over 63 days. Higher = more stable beta."""
    validate_prices(prices)
    wide = to_wide(prices)
    if _BENCHMARK not in wide.columns:
        raise ValueError("beta_stability_63d requires 'SPY' to be present in prices")
    daily_ret = wide.pct_change()
    beta_21d = rolling_beta(daily_ret, _BENCHMARK, _SHORT_WINDOW, _SHORT_MIN)
    beta_21d = beta_21d.drop(columns=[_BENCHMARK], errors="ignore")
    beta_std = beta_21d.rolling(_OUTER_WINDOW, min_periods=_OUTER_MIN).std()
    z = cross_sectional_zscore(-beta_std)
    result = to_long(z, "beta_stability_63d_score")
    logger.info("beta_stability_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

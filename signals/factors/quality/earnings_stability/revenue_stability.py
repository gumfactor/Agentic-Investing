"""Revenue stability factor.

−CoV(Revenue_TTM over 8 quarters) = −std / mean.
Revenue is less subject to accounting discretion than earnings, making its
coefficient of variation a cleaner signal of business model predictability.
Cyclical businesses and project-based companies score low; subscription
and recurring-revenue businesses score high.
Requires at least 4 valid quarterly observations.
Negated so that higher score = more predictable revenue.

Requires fundamentals column: revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)

_WINDOW = 8
_MIN_OBS = 4


def compute_revenue_stability_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −std(Revenue) / mean(Revenue). Higher = more predictable revenue."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm"})
    price_wide = to_wide(prices)
    rev_wide = fund_to_wide(fundamentals, "revenue_ttm")
    rolling_std = rev_wide.rolling(_WINDOW, min_periods=_MIN_OBS).std()
    rolling_mean = rev_wide.rolling(_WINDOW, min_periods=_MIN_OBS).mean()
    cov = rolling_std / rolling_mean.where(rolling_mean > 0)
    stability = align_fundamentals(-cov, price_wide.index)
    z = cross_sectional_zscore(stability)
    result = to_long(z, "revenue_stability_score")
    logger.info("revenue_stability_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

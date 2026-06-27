"""EPS stability factor.

−CoV(EPS_TTM over 8 quarters) = −std / |mean|.
Coefficient of variation captures relative volatility, making it
cross-sectionally comparable across large and small earners. Rolling is
computed on the sparse quarterly series before alignment, so each
observation reflects actual quarterly data rather than forward-fill artifacts.
Requires at least 4 valid quarterly observations.
Negated so that higher score = more stable earnings stream.

Requires fundamentals column: eps_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)

_WINDOW = 8
_MIN_OBS = 4
_MEAN_FLOOR = 0.01


def compute_eps_stability_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −std(EPS) / |mean(EPS)|. Higher = more stable earnings."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_ttm"})
    price_wide = to_wide(prices)
    eps_wide = fund_to_wide(fundamentals, "eps_ttm")
    rolling_std = eps_wide.rolling(_WINDOW, min_periods=_MIN_OBS).std()
    rolling_mean = eps_wide.rolling(_WINDOW, min_periods=_MIN_OBS).mean().abs()
    cov = rolling_std / rolling_mean.where(rolling_mean > _MEAN_FLOOR)
    stability = align_fundamentals(-cov, price_wide.index)
    z = cross_sectional_zscore(stability)
    result = to_long(z, "eps_stability_score")
    logger.info("eps_stability_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""EPS growth acceleration factor.

YoY_EPS_growth_now − YoY_EPS_growth_lag63d.
The second derivative of earnings: measures whether the rate of EPS growth
is itself speeding up or slowing down. Catches inflection points ahead of
the broader market's recognition of the trend change.
Only defined where both growth rates have positive bases.
Higher = earnings growth is accelerating.

Requires fundamentals column: eps_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252
_LAG_63D = 63


def compute_eps_growth_acceleration_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of change in YoY EPS growth rate. Higher = accelerating earnings."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_ttm"})
    price_wide = to_wide(prices)
    eps = align_fundamentals(fund_to_wide(fundamentals, "eps_ttm"), price_wide.index)
    base_now = eps.shift(_LAG_1Y)
    base_prior = eps.shift(_LAG_1Y + _LAG_63D)
    growth_now = (eps - base_now) / base_now.where(base_now > 0)
    growth_prior = (eps.shift(_LAG_63D) - base_prior) / base_prior.where(base_prior > 0)
    acceleration = growth_now - growth_prior
    z = cross_sectional_zscore(acceleration)
    result = to_long(z, "eps_growth_acceleration_score")
    logger.info("eps_growth_acceleration_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

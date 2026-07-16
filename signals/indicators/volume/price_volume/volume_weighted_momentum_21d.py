"""21-day volume-weighted momentum factor.

Average daily return over 21 days, with each day weighted by its relative
volume (volume / mean_volume). High-volume price moves count more than
low-volume drifts. Captures conviction-backed momentum.
Higher = price moves consistently upward on above-average volume.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return
from signals.indicators._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 21  # full window (BUG-010); daily_ret*rel_vol already propagates NaN, so this alone suffices
_NORM_WINDOW = 63  # volume-only normalization window; not a return statistic, left as-is (see 01B-1 inventory)


def compute_volume_weighted_momentum_21d_scores(
    prices: pd.DataFrame,
    volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of volume-weighted avg daily return over 21 days."""
    validate_prices(prices)
    validate_volumes(volumes)
    price_wide = to_wide(prices)
    vol_wide = vol_to_wide(volumes).reindex(index=price_wide.index, columns=price_wide.columns)
    daily_ret = daily_return(price_wide)
    mean_vol = vol_wide.rolling(_NORM_WINDOW, min_periods=44).mean()
    rel_vol = vol_wide / mean_vol.where(mean_vol > 0)
    vw_mom = (daily_ret * rel_vol).rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    z = cross_sectional_zscore(vw_mom)
    result = to_long(z, "volume_weighted_momentum_21d_score")
    logger.info("volume_weighted_momentum_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

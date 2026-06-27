"""21-day up/down volume ratio factor.

Sum of volume on positive-return days / sum of volume on negative-return days
over the past 21 trading days.
Values above 1.0 = more volume traded on up days than down days (buying pressure).
Higher = more buying-side volume dominance.
Requires both prices and volumes DataFrames.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 15


def compute_volume_up_down_ratio_21d_scores(
    prices: pd.DataFrame,
    volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of up-volume / down-volume over 21 days. Higher = buying pressure."""
    validate_prices(prices)
    validate_volumes(volumes)
    price_wide = to_wide(prices)
    vol_wide = vol_to_wide(volumes).reindex(index=price_wide.index, columns=price_wide.columns)
    daily_ret = price_wide.pct_change()
    up_mask = (daily_ret > 0).astype(float)
    down_mask = (daily_ret < 0).astype(float)
    up_vol = (vol_wide * up_mask).rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
    down_vol = (vol_wide * down_mask).rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
    ratio = up_vol / down_vol.where(down_vol > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "volume_up_down_ratio_21d_score")
    logger.info("volume_up_down_ratio_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

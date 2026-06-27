"""21-day volume trend slope factor.

Linear slope of a 5-day smoothed volume series over 21 days,
normalised by mean volume so the result is dimensionless.
Positive = volume is trending upward (growing interest).
Negative = volume is drying up (waning interest).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import to_long, cross_sectional_zscore
from signals.indicators._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 15


def _slope(x: np.ndarray) -> float:
    mask = ~np.isnan(x)
    if mask.sum() < 10:
        return np.nan
    t = np.arange(len(x), dtype=float)[mask]
    slope, _ = np.polyfit(t, x[mask], 1)
    return slope


def compute_volume_trend_slope_21d_scores(volumes: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of linear slope of 21d volume, normalised by mean volume."""
    validate_volumes(volumes)
    vol = vol_to_wide(volumes)
    vol_smooth = vol.rolling(5, min_periods=3).mean()
    slope = vol_smooth.rolling(_WINDOW, min_periods=_MIN_PERIODS).apply(_slope, raw=True)
    mean_vol = vol.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    norm_slope = slope / mean_vol.where(mean_vol > 0)
    z = cross_sectional_zscore(norm_slope)
    result = to_long(z, "volume_trend_slope_21d_score")
    logger.info("volume_trend_slope_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

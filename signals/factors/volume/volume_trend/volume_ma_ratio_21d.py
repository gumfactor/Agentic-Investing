"""21-day volume MA ratio factor.

Current volume / 21-day SMA of volume.
Values above 1.0 = today's volume is above the 1-month average (unusual activity).
Higher = more anomalous volume — attention signal.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 15


def compute_volume_ma_ratio_21d_scores(volumes: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of volume / SMA(volume, 21). Higher = more unusual volume spike."""
    validate_volumes(volumes)
    vol = vol_to_wide(volumes)
    sma = vol.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    ratio = vol / sma.where(sma > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "volume_ma_ratio_21d_score")
    logger.info("volume_ma_ratio_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

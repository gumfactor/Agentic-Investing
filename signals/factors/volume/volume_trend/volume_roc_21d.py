"""21-day volume rate of change factor.

(Volume_today - Volume_21d_ago) / Volume_21d_ago.
Measures how much volume has changed vs one month ago.
Higher = volume has expanded significantly — growing market interest.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_LOOKBACK = 21


def compute_volume_roc_21d_scores(volumes: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day volume rate of change. Higher = volume expanded vs 1 month ago."""
    validate_volumes(volumes)
    vol = vol_to_wide(volumes)
    roc = vol / vol.shift(_LOOKBACK) - 1
    z = cross_sectional_zscore(roc)
    result = to_long(z, "volume_roc_21d_score")
    logger.info("volume_roc_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

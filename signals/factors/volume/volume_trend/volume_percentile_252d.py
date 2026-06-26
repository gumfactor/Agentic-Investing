"""Annual volume percentile factor.

Percentile rank of current volume within its own 252-day history (0–1).
0.0 = quietest day in a year; 1.0 = highest-volume day in a year.
Higher = unusually high volume vs own annual range — elevated market attention.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)


def compute_volume_percentile_252d_scores(volumes: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of volume percentile rank in own 252-day history."""
    validate_volumes(volumes)
    vol = vol_to_wide(volumes)
    pct_rank = vol.rolling(252, min_periods=126).rank(pct=True)
    z = cross_sectional_zscore(pct_rank)
    result = to_long(z, "volume_percentile_252d_score")
    logger.info("volume_percentile_252d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

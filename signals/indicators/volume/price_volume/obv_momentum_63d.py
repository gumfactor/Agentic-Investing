"""63-day On-Balance Volume momentum factor.

Slower, smoother variant of OBV momentum measured over a full quarter.
OBV change over 63 days / mean daily volume — how many days' worth of net
volume has been accumulated over the past quarter.
Higher = sustained accumulation over the quarter.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_LOOKBACK = 63
_NORM_WINDOW = 63


def compute_obv_momentum_63d_scores(
    prices: pd.DataFrame,
    volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day OBV change / mean daily volume. Higher = sustained accumulation."""
    validate_prices(prices)
    validate_volumes(volumes)
    price_wide = to_wide(prices)
    vol_wide = vol_to_wide(volumes).reindex(index=price_wide.index, columns=price_wide.columns)
    sign = np.sign(price_wide.pct_change())
    obv = (vol_wide * sign).cumsum()
    mean_vol = vol_wide.rolling(_NORM_WINDOW, min_periods=44).mean()
    obv_mom = (obv - obv.shift(_LOOKBACK)) / mean_vol.where(mean_vol > 0)
    z = cross_sectional_zscore(obv_mom)
    result = to_long(z, "obv_momentum_63d_score")
    logger.info("obv_momentum_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

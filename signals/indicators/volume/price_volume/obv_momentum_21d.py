"""21-day On-Balance Volume momentum factor.

OBV = cumulative sum of (+volume on up days, -volume on down days).
OBV momentum = change in OBV over 21 days, normalised by mean daily volume
so values are dimensionless and cross-sectionally comparable.
Positive = net accumulation accelerating; negative = distribution.
Higher = stronger recent accumulation trend.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import (
    validate_prices,
    to_wide,
    to_long,
    cross_sectional_zscore,
    daily_return,
    require_full_window,
)
from signals.indicators._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_LOOKBACK = 21
_NORM_WINDOW = 63  # volume-only normalization window; not a return statistic, left as-is (see 01B-1 inventory)


def compute_obv_momentum_21d_scores(
    prices: pd.DataFrame,
    volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day OBV change / mean daily volume. Higher = net accumulation."""
    validate_prices(prices)
    validate_volumes(volumes)
    price_wide = to_wide(prices)
    vol_wide = vol_to_wide(volumes).reindex(index=price_wide.index, columns=price_wide.columns)
    daily_ret = daily_return(price_wide)
    sign = np.sign(daily_ret)
    # obv is a cumulative sum: cumsum() treats NaN as a 0 contribution
    # (skipna=True), so a missing return does not itself turn obv into NaN.
    # Gate the LOOKBACK-day delta explicitly on the trailing window having a
    # full set of valid returns (BUG-010).
    obv = (vol_wide * sign).cumsum()
    mean_vol = vol_wide.rolling(_NORM_WINDOW, min_periods=44).mean()
    obv_mom = (obv - obv.shift(_LOOKBACK)) / mean_vol.where(mean_vol > 0)
    obv_mom = require_full_window(obv_mom, daily_ret, _LOOKBACK)
    z = cross_sectional_zscore(obv_mom)
    result = to_long(z, "obv_momentum_21d_score")
    logger.info("obv_momentum_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

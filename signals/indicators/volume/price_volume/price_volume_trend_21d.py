"""21-day Price-Volume Trend (PVT) momentum factor.

PVT = cumulative sum of (daily_return × volume).
Unlike OBV, PVT scales by the size of the price move, not just direction.
PVT momentum = 21-day change in PVT, normalised by mean daily (return × volume)
to be dimensionless and cross-sectionally comparable.
Higher = strong recent accumulation with large price moves.
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
_NORM_WINDOW = 63  # mean_flow is return-derived (daily_ret.abs()), so NaN propagates and full min_periods applies


def compute_price_volume_trend_21d_scores(
    prices: pd.DataFrame,
    volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day PVT change normalised by mean daily flow."""
    validate_prices(prices)
    validate_volumes(volumes)
    price_wide = to_wide(prices)
    vol_wide = vol_to_wide(volumes).reindex(index=price_wide.index, columns=price_wide.columns)
    daily_ret = daily_return(price_wide)
    # pvt is a cumulative sum: cumsum() treats NaN as a 0 contribution
    # (skipna=True), so a missing return does not itself turn pvt into NaN.
    # Gate the LOOKBACK-day delta explicitly on the trailing window having a
    # full set of valid returns (BUG-010).
    pvt = (daily_ret * vol_wide).cumsum()
    mean_flow = (daily_ret.abs() * vol_wide).rolling(_NORM_WINDOW, min_periods=_NORM_WINDOW).mean()
    pvt_mom = (pvt - pvt.shift(_LOOKBACK)) / mean_flow.where(mean_flow > 0)
    pvt_mom = require_full_window(pvt_mom, daily_ret, _LOOKBACK)
    z = cross_sectional_zscore(pvt_mom)
    result = to_long(z, "price_volume_trend_21d_score")
    logger.info("price_volume_trend_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

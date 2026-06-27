"""13-day Force Index factor.

Force Index = EMA(13) of (daily_return × volume).
Measures the power behind a price move: large return on high volume = strong force.
Normalised by mean daily volume so values are in units of return, making it
comparable across stocks with different volume scales.
Higher = net buying force over recent sessions.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_SPAN = 13
_NORM_WINDOW = 63


def compute_force_index_13d_scores(
    prices: pd.DataFrame,
    volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of EMA(13) of (return × volume) / mean_volume. Higher = net buying force."""
    validate_prices(prices)
    validate_volumes(volumes)
    price_wide = to_wide(prices)
    vol_wide = vol_to_wide(volumes).reindex(index=price_wide.index, columns=price_wide.columns)
    daily_ret = price_wide.pct_change()
    force_raw = daily_ret * vol_wide
    force_ema = force_raw.ewm(span=_SPAN, adjust=False).mean()
    mean_vol = vol_wide.rolling(_NORM_WINDOW, min_periods=44).mean()
    force_norm = force_ema / mean_vol.where(mean_vol > 0)
    z = cross_sectional_zscore(force_norm)
    result = to_long(z, "force_index_13d_score")
    logger.info("force_index_13d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

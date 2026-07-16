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
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return
from signals.indicators._volume_utils import validate_volumes, vol_to_wide

logger = structlog.get_logger(__name__)

_SPAN = 13
_NORM_WINDOW = 63  # volume-only normalization window; not a return statistic, left as-is (see 01B-1 inventory)


def compute_force_index_13d_scores(
    prices: pd.DataFrame,
    volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of EMA(13) of (return × volume) / mean_volume. Higher = net buying force.

    force_raw feeds an EWM (exponential decay), not a fixed N-lookback
    window, so pct_change's default fill_method is the only migration
    needed here: EWM has no closed calendar boundary to gate with
    require_full_window against, and pandas' default ignore_na=False
    (time-decay, not count-decay) is the standard convention for this
    estimator family. See the 01B-1 inventory for the full rationale.
    """
    validate_prices(prices)
    validate_volumes(volumes)
    price_wide = to_wide(prices)
    vol_wide = vol_to_wide(volumes).reindex(index=price_wide.index, columns=price_wide.columns)
    daily_ret = daily_return(price_wide)
    force_raw = daily_ret * vol_wide
    force_ema = force_raw.ewm(span=_SPAN, adjust=False).mean()
    mean_vol = vol_wide.rolling(_NORM_WINDOW, min_periods=44).mean()
    force_norm = force_ema / mean_vol.where(mean_vol > 0)
    z = cross_sectional_zscore(force_norm)
    result = to_long(z, "force_index_13d_score")
    logger.info("force_index_13d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

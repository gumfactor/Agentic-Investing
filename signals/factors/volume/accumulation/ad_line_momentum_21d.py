"""21-day Accumulation/Distribution Line momentum factor.

A/D Line = cumulative sum of (CLV × volume), where
CLV = (2×Close − High − Low) / (High − Low).
CLV = +1 when close is at the high (max accumulation);
CLV = −1 when close is at the low (max distribution).
21-day momentum = change in A/D line normalised by mean daily volume.
Higher = institutional accumulation accelerating.
Requires ohlcv DataFrame with columns [date, ticker, open, high, low, close, volume].
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._ohlc_utils import validate_ohlcv, ohlc_wide

logger = structlog.get_logger(__name__)

_LOOKBACK = 21
_NORM_WINDOW = 63


def compute_ad_line_momentum_21d_scores(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day A/D line change / mean volume. Higher = accumulation momentum."""
    validate_ohlcv(ohlcv)
    high = ohlc_wide(ohlcv, "high")
    low = ohlc_wide(ohlcv, "low")
    close = ohlc_wide(ohlcv, "close")
    vol = ohlc_wide(ohlcv, "volume")
    hl = high - low
    clv = (2 * close - high - low) / hl.where(hl > 0)
    ad_line = (clv * vol).cumsum()
    mean_vol = vol.rolling(_NORM_WINDOW, min_periods=44).mean()
    ad_mom = (ad_line - ad_line.shift(_LOOKBACK)) / mean_vol.where(mean_vol > 0)
    z = cross_sectional_zscore(ad_mom)
    result = to_long(z, "ad_line_momentum_21d_score")
    logger.info("ad_line_momentum_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

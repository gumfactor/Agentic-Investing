"""Chaikin Oscillator factor.

EMA(3) − EMA(10) of the Accumulation/Distribution Line.
Positive = A/D line's short-term EMA is above its medium-term EMA:
accumulation momentum is accelerating.
Normalised by mean daily volume for cross-sectional comparability.
Requires ohlcv DataFrame with columns [date, ticker, open, high, low, close, volume].
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import to_long, cross_sectional_zscore
from signals.indicators._ohlc_utils import validate_ohlcv, ohlc_wide

logger = structlog.get_logger(__name__)

_NORM_WINDOW = 63


def compute_chaikin_oscillator_scores(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of Chaikin Oscillator (EMA3 - EMA10 of A/D line)."""
    validate_ohlcv(ohlcv)
    high = ohlc_wide(ohlcv, "high")
    low = ohlc_wide(ohlcv, "low")
    close = ohlc_wide(ohlcv, "close")
    vol = ohlc_wide(ohlcv, "volume")
    hl = high - low
    clv = (2 * close - high - low) / hl.where(hl > 0)
    ad_line = (clv * vol).cumsum()
    osc = ad_line.ewm(span=3, adjust=False).mean() - ad_line.ewm(span=10, adjust=False).mean()
    mean_vol = vol.rolling(_NORM_WINDOW, min_periods=44).mean()
    osc_norm = osc / mean_vol.where(mean_vol > 0)
    z = cross_sectional_zscore(osc_norm)
    result = to_long(z, "chaikin_oscillator_score")
    logger.info("chaikin_oscillator_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

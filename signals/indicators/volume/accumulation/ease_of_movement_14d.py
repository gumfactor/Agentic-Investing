"""14-day Ease of Movement (EOM) factor.

EOM = (midpoint_change × high_low_range) / volume
where midpoint = (High + Low) / 2.
Positive EOM = price moved up easily on low volume.
Negative EOM = price moved down or required high volume to move up.
Normalised by close price for cross-sectional comparability.
Higher = price moves upward with little volume effort — low-resistance rally.
Requires ohlcv DataFrame with columns [date, ticker, open, high, low, close, volume].
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import to_long, cross_sectional_zscore
from signals.indicators._ohlc_utils import validate_ohlcv, ohlc_wide

logger = structlog.get_logger(__name__)

_SPAN = 14


def compute_ease_of_movement_14d_scores(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of EMA(14) of EOM / close. Higher = effortless upward price movement."""
    validate_ohlcv(ohlcv)
    high = ohlc_wide(ohlcv, "high")
    low = ohlc_wide(ohlcv, "low")
    close = ohlc_wide(ohlcv, "close")
    vol = ohlc_wide(ohlcv, "volume")
    midpoint = (high + low) / 2
    midpoint_change = midpoint - midpoint.shift(1)
    hl = high - low
    box_ratio = vol / hl.where(hl > 0)
    eom_raw = midpoint_change / box_ratio.where(box_ratio > 0)
    eom_norm = eom_raw / close.where(close > 0)
    eom = eom_norm.ewm(span=_SPAN, adjust=False).mean()
    z = cross_sectional_zscore(eom)
    result = to_long(z, "ease_of_movement_14d_score")
    logger.info("ease_of_movement_14d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

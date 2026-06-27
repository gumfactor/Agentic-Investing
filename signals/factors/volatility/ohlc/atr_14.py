"""14-day ATR (Average True Range) factor.

True Range = max(H−L, |H−C_prev|, |L−C_prev|).
ATR(14) = 14-day rolling mean of True Range.
Normalised by close price so it represents the typical daily move as a
fraction of price — cross-sectionally comparable across price levels.
Higher = larger typical daily range = more volatile.
Requires ohlc DataFrame with columns [date, ticker, open, high, low, close].
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._ohlc_utils import validate_ohlc, ohlc_wide

logger = structlog.get_logger(__name__)

_WINDOW = 14
_MIN_PERIODS = 10


def compute_atr_14_scores(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of ATR(14) / close. Higher = larger typical daily range."""
    validate_ohlc(ohlc)
    high = ohlc_wide(ohlc, "high")
    low = ohlc_wide(ohlc, "low")
    close = ohlc_wide(ohlc, "close")
    prev_close = close.shift(1)
    hl = high - low
    hc = (high - prev_close).abs()
    lc = (low - prev_close).abs()
    tr = pd.DataFrame(
        np.maximum(hl.values, np.maximum(hc.values, lc.values)),
        index=hl.index, columns=hl.columns,
    )
    atr = tr.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    atr_pct = atr / close.where(close > 0)
    z = cross_sectional_zscore(atr_pct)
    result = to_long(z, "atr_14_score")
    logger.info("atr_14_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

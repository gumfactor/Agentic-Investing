"""ATR ratio factor: ATR(14) / ATR(63).

Compares the short-term true range to the medium-term baseline.
Values above 1.0 = current daily swings expanded vs recent norm (vol spike).
Values below 1.0 = daily range contracted (vol compression before breakout).
Higher = short-term range expansion.
Requires ohlc DataFrame with columns [date, ticker, open, high, low, close].
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import to_long, cross_sectional_zscore
from signals.indicators._ohlc_utils import validate_ohlc, ohlc_wide

logger = structlog.get_logger(__name__)


def compute_atr_ratio_14_63_scores(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of ATR(14) / ATR(63). Higher = short-term range expanded."""
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
    atr_14 = tr.rolling(14, min_periods=10).mean()
    atr_63 = tr.rolling(63, min_periods=44).mean()
    ratio = atr_14 / atr_63.where(atr_63 > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "atr_ratio_14_63_score")
    logger.info("atr_ratio_14_63_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

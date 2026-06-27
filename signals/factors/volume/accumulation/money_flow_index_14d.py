"""14-day Money Flow Index (MFI) factor.

MFI is a volume-weighted RSI using typical price (TP = (H+L+C)/3).
Positive money flow = TP > prev TP; negative = TP < prev TP.
MFI = 100 − 100 / (1 + positive_money_flow / negative_money_flow).
Range 0–100. Higher = stronger volume-confirmed buying pressure.
Requires ohlcv DataFrame with columns [date, ticker, open, high, low, close, volume].
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._ohlc_utils import validate_ohlcv, ohlc_wide

logger = structlog.get_logger(__name__)

_WINDOW = 14
_MIN_PERIODS = 10


def compute_money_flow_index_14d_scores(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of MFI(14). Higher = stronger volume-confirmed buying pressure."""
    validate_ohlcv(ohlcv)
    high = ohlc_wide(ohlcv, "high")
    low = ohlc_wide(ohlcv, "low")
    close = ohlc_wide(ohlcv, "close")
    vol = ohlc_wide(ohlcv, "volume")
    tp = (high + low + close) / 3
    raw_mf = tp * vol
    tp_change = tp - tp.shift(1)
    pos_mf = raw_mf.where(tp_change > 0, 0.0)
    neg_mf = raw_mf.where(tp_change < 0, 0.0).abs()
    pos_sum = pos_mf.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
    neg_sum = neg_mf.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
    mfr = pos_sum / neg_sum.where(neg_sum > 0)
    mfi = 100 - (100 / (1 + mfr))
    z = cross_sectional_zscore(mfi)
    result = to_long(z, "money_flow_index_14d_score")
    logger.info("money_flow_index_14d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

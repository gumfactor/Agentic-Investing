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
from signals.indicators._price_utils import to_long, cross_sectional_zscore, require_full_window
from signals.indicators._ohlc_utils import validate_ohlcv, ohlc_wide

logger = structlog.get_logger(__name__)

_WINDOW = 14
_MIN_PERIODS = 14  # full window (BUG-010); see also require_full_window gate below


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
    # NaN tp_change (a gap day, or the day after — its diff spans the gap)
    # compares False to both `> 0` and `< 0`, so `.where(..., 0.0)` fabricates
    # a zero flow for the missing session that the rolling sums quietly
    # absorb — min_periods alone cannot catch this, since the summed series
    # itself is never NaN. Gate the final MFI on the trailing window having a
    # full set of valid tp_change values (BUG-010) — same treatment as
    # volume_up_down_ratio. See docs/plans/01b1-pct-change-inventory.md.
    pos_mf = raw_mf.where(tp_change > 0, 0.0)
    neg_mf = raw_mf.where(tp_change < 0, 0.0).abs()
    pos_sum = pos_mf.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
    neg_sum = neg_mf.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
    mfr = pos_sum / neg_sum.where(neg_sum > 0)
    mfi = 100 - (100 / (1 + mfr))
    mfi = require_full_window(mfi, tp_change, _WINDOW)
    z = cross_sectional_zscore(mfi)
    result = to_long(z, "money_flow_index_14d_score")
    logger.info("money_flow_index_14d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

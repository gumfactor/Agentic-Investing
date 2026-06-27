"""21-day Chaikin Money Flow (CMF) factor.

CMF = Σ(CLV × volume, 21d) / Σ(volume, 21d)
where CLV = (2×Close − High − Low) / (High − Low).
Range: −1 to +1. Positive = net money flowing in; negative = net outflow.
Self-normalised by volume, so no further scaling needed.
Higher = stronger net buying pressure over the past month.
Requires ohlcv DataFrame with columns [date, ticker, open, high, low, close, volume].
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._ohlc_utils import validate_ohlcv, ohlc_wide

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 15


def compute_chaikin_money_flow_21d_scores(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of CMF(21). Higher = stronger net buying pressure."""
    validate_ohlcv(ohlcv)
    high = ohlc_wide(ohlcv, "high")
    low = ohlc_wide(ohlcv, "low")
    close = ohlc_wide(ohlcv, "close")
    vol = ohlc_wide(ohlcv, "volume")
    hl = high - low
    clv = (2 * close - high - low) / hl.where(hl > 0)
    money_flow_vol = clv * vol
    cmf = (
        money_flow_vol.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
        / vol.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum().where(
            vol.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum() > 0
        )
    )
    z = cross_sectional_zscore(cmf)
    result = to_long(z, "chaikin_money_flow_21d_score")
    logger.info("chaikin_money_flow_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

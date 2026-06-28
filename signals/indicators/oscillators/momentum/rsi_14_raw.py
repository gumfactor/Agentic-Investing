"""14-day RSI — raw absolute value.

Outputs the actual RSI value (0–100) without cross-sectional z-scoring,
so absolute overbought/oversold thresholds are preserved across dates.

Use this variant when the strategy cares about absolute RSI levels:
  - RSI < 30: classically oversold (contrarian BUY signal)
  - RSI > 70: classically overbought (contrarian SELL signal)

For cross-sectional relative momentum (higher RSI = more momentum than
peers), use rsi_14.py instead.
"""
from __future__ import annotations

import pandas as pd
import structlog

from signals.indicators._price_utils import validate_prices, to_wide, to_long

logger = structlog.get_logger(__name__)

_PERIOD = 14


def _rsi(wide: pd.DataFrame, period: int) -> pd.DataFrame:
    delta = wide.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.where(avg_loss > 0)
    rsi = 100 - (100 / (1 + rs))
    # Pure uptrend (avg_loss=0, past warmup): avg_gain / NaN → NaN → fill with 100
    pure_uptrend = avg_gain.notna() & (avg_loss == 0)
    rsi = rsi.where(~pure_uptrend, other=100.0)
    return rsi


def compute_rsi_14_raw_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Raw RSI(14) value (0–100). Not cross-sectionally z-scored.

    Lower values indicate oversold conditions in absolute terms;
    higher values indicate overbought conditions.
    """
    validate_prices(prices)
    wide = to_wide(prices)
    rsi = _rsi(wide, _PERIOD)
    result = to_long(rsi, "rsi_14_raw")
    logger.info(
        "rsi_14_raw_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
    )
    return result

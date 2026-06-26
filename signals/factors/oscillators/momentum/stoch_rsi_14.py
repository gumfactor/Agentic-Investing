"""14-day Stochastic RSI factor.

StochRSI = (RSI(14) - RSI_min(14)) / (RSI_max(14) - RSI_min(14)).
Applies the Stochastic formula to RSI values instead of price.
More sensitive than plain RSI; reaches extremes more quickly.
Range 0–1. Higher = RSI near top of its 14-period range = strong momentum.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_RSI_PERIOD = 14
_STOCH_WINDOW = 14
_MIN_PERIODS = 10


def _rsi(wide: pd.DataFrame, period: int) -> pd.DataFrame:
    delta = wide.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.where(avg_loss > 0)
    return 100 - (100 / (1 + rs))


def compute_stoch_rsi_14_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of StochRSI(14). Higher = RSI near its 14-period high."""
    validate_prices(prices)
    wide = to_wide(prices)
    rsi = _rsi(wide, _RSI_PERIOD)
    rsi_min = rsi.rolling(_STOCH_WINDOW, min_periods=_MIN_PERIODS).min()
    rsi_max = rsi.rolling(_STOCH_WINDOW, min_periods=_MIN_PERIODS).max()
    rsi_range = rsi_max - rsi_min
    stoch_rsi = (rsi - rsi_min) / rsi_range.where(rsi_range > 0)
    z = cross_sectional_zscore(stoch_rsi)
    result = to_long(z, "stoch_rsi_14_score")
    logger.info("stoch_rsi_14_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""28-day RSI factor.

Relative Strength Index over 28 periods using Wilder's smoothing.
Slower, smoother variant of RSI(14); less prone to whipsaws.
Range 0–100. Higher = stronger sustained buying pressure.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_PERIOD = 28


def _rsi(wide: pd.DataFrame, period: int) -> pd.DataFrame:
    delta = wide.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.where(avg_loss > 0)
    return 100 - (100 / (1 + rs))


def compute_rsi_28_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of RSI(28). Higher = stronger sustained momentum."""
    validate_prices(prices)
    wide = to_wide(prices)
    rsi = _rsi(wide, _PERIOD)
    z = cross_sectional_zscore(rsi)
    result = to_long(z, "rsi_28_score")
    logger.info("rsi_28_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""14-day RSI factor.

Relative Strength Index over 14 periods using Wilder's smoothing.
Range 0–100. Higher = stronger recent buying pressure = more momentum.
Values above ~70 are classically overbought; below ~30 are oversold.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, require_full_window

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
    # BUG-010 EWM gate: pandas EWM with the default ignore_na=False decays
    # *through* a NaN input (a missing session's delta), so on a gap day —
    # and on the day after, whose diff spans the gap — the smoothed averages
    # are silently carried forward and RSI would emit a frozen duplicate of
    # the prior value. Suppress RSI wherever the trailing `period` deltas
    # (the estimator's nominal window, matching its min_periods warm-up)
    # contain a gap. See docs/plans/01b1-pct-change-inventory.md.
    return require_full_window(rsi, delta, period)


def compute_rsi_14_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of RSI(14). Higher = stronger recent momentum."""
    validate_prices(prices)
    wide = to_wide(prices)
    rsi = _rsi(wide, _PERIOD)
    z = cross_sectional_zscore(rsi)
    result = to_long(z, "rsi_14_score")
    logger.info("rsi_14_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

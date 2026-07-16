"""Chaikin Oscillator factor.

EMA(3) − EMA(10) of the Accumulation/Distribution Line.
Positive = A/D line's short-term EMA is above its medium-term EMA:
accumulation momentum is accelerating.
Normalised by mean daily volume for cross-sectional comparability.
Requires ohlcv DataFrame with columns [date, ticker, open, high, low, close, volume].
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import to_long, cross_sectional_zscore, require_full_window
from signals.indicators._ohlc_utils import validate_ohlcv, ohlc_wide

logger = structlog.get_logger(__name__)

_NORM_WINDOW = 63  # volume-only normalization window; not a flow statistic (see 01B-1 inventory)
_SLOW_SPAN = 10
_FAST_SPAN = 3


def compute_chaikin_oscillator_scores(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of Chaikin Oscillator (EMA3 - EMA10 of A/D line)."""
    validate_ohlcv(ohlcv)
    high = ohlc_wide(ohlcv, "high")
    low = ohlc_wide(ohlcv, "low")
    close = ohlc_wide(ohlcv, "close")
    vol = ohlc_wide(ohlcv, "volume")
    hl = high - low
    clv = (2 * close - high - low) / hl.where(hl > 0)
    flow = clv * vol
    # ad_line is a cumulative sum: cumsum() treats NaN as a 0 contribution
    # (skipna=True), so a missing session's flow does not itself turn the
    # A/D line into NaN, and the EWMs layered on top (ignore_na=False) decay
    # straight through the gap. Gate the oscillator on the trailing
    # `_SLOW_SPAN` flow inputs (the slower EMA's nominal window) being
    # gap-free (BUG-010). See docs/plans/01b1-pct-change-inventory.md.
    ad_line = flow.cumsum()
    osc = ad_line.ewm(span=_FAST_SPAN, adjust=False).mean() - ad_line.ewm(span=_SLOW_SPAN, adjust=False).mean()
    mean_vol = vol.rolling(_NORM_WINDOW, min_periods=44).mean()
    osc_norm = osc / mean_vol.where(mean_vol > 0)
    osc_norm = require_full_window(osc_norm, flow, _SLOW_SPAN)
    z = cross_sectional_zscore(osc_norm)
    result = to_long(z, "chaikin_oscillator_score")
    logger.info("chaikin_oscillator_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

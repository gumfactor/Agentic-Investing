"""21-day Accumulation/Distribution Line momentum factor.

A/D Line = cumulative sum of (CLV × volume), where
CLV = (2×Close − High − Low) / (High − Low).
CLV = +1 when close is at the high (max accumulation);
CLV = −1 when close is at the low (max distribution).
21-day momentum = change in A/D line normalised by mean daily volume.
Higher = institutional accumulation accelerating.
Requires ohlcv DataFrame with columns [date, ticker, open, high, low, close, volume].
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import to_long, cross_sectional_zscore, require_full_window
from signals.indicators._ohlc_utils import validate_ohlcv, ohlc_wide

logger = structlog.get_logger(__name__)

_LOOKBACK = 21
_NORM_WINDOW = 63  # volume-only normalization window; not a flow statistic (see 01B-1 inventory)


def compute_ad_line_momentum_21d_scores(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day A/D line change / mean volume. Higher = accumulation momentum."""
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
    # A/D line into NaN and the LOOKBACK-day delta would silently recover
    # as if the gap never happened. Gate the delta on the trailing
    # `_LOOKBACK` flow inputs being gap-free (BUG-010) — same treatment as
    # OBV/PVT. See docs/plans/01b1-pct-change-inventory.md.
    ad_line = flow.cumsum()
    mean_vol = vol.rolling(_NORM_WINDOW, min_periods=44).mean()
    ad_mom = (ad_line - ad_line.shift(_LOOKBACK)) / mean_vol.where(mean_vol > 0)
    ad_mom = require_full_window(ad_mom, flow, _LOOKBACK)
    z = cross_sectional_zscore(ad_mom)
    result = to_long(z, "ad_line_momentum_21d_score")
    logger.info("ad_line_momentum_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""Bollinger Band %B (20-day, 2-sigma) — raw absolute value.

Outputs the actual %B value without cross-sectional z-scoring, so
absolute band-position thresholds are preserved across dates.

%B = (Close − Lower Band) / (Upper Band − Lower Band)

  %B ≈ 0   : price at or near the lower Bollinger Band (oversold)
  %B = 0.5 : price at the 20-day SMA (neutral)
  %B ≈ 1   : price at or near the upper Bollinger Band (overbought)
  %B < 0   : price below the lower band (extremely oversold)
  %B > 1   : price above the upper band (extremely overbought)

Use this variant when the strategy applies absolute %B thresholds
(e.g., %B < 0.2 as a mean-reversion entry trigger).

For cross-sectional relative momentum (which ticker is nearest its upper
band vs peers), use bb_pct_b_20.py instead.
"""
from __future__ import annotations

import pandas as pd
import structlog

from signals.indicators._price_utils import validate_prices, to_wide, to_long, compute_sma

logger = structlog.get_logger(__name__)

_WINDOW = 20
_MIN_PERIODS = 14
_N_STD = 2.0


def compute_bb_pct_b_20_raw_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Raw Bollinger %B(20, 2σ). Not cross-sectionally z-scored.

    Values near 0 indicate price near the lower band (oversold);
    values near 1 indicate price near the upper band (overbought).
    """
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=_WINDOW)
    std = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).std()
    upper = sma + _N_STD * std
    lower = sma - _N_STD * std
    band_width = upper - lower
    pct_b = (wide - lower) / band_width.where(band_width > 0)
    result = to_long(pct_b, "bb_pct_b_20_raw")
    logger.info(
        "bb_pct_b_20_raw_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
    )
    return result

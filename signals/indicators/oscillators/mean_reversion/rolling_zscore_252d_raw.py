"""252-day rolling Z-score — raw time-series value.

Z = (Close − mean(252d)) / std(252d)

Outputs the time-series z-score of price vs its own 252-day history,
without a second cross-sectional z-score pass. This preserves the absolute
deviation from the stock's own annual average.

  Z ≈ −2 : price is ~2 standard deviations below its 252-day mean (oversold
            vs own history — a contrarian BUY candidate)
  Z ≈  0 : price near its annual mean (neutral)
  Z ≈ +2 : price ~2 standard deviations above its annual mean (extended)

Important: this is a self-referential measure — it says nothing about where
the stock stands relative to peers. Two stocks can have Z = −2 while one is
fundamentally distressed and the other is a high-quality temporary dip.

Use this variant for contrarian / mean-reversion composites that need to
know how far price has moved from the stock's own baseline, in absolute
standard-deviation units.

For a cross-sectional signal (which stock has deviated most from its own
history vs peers), use rolling_zscore_252d.py instead.
"""
from __future__ import annotations

import pandas as pd
import structlog

from signals.indicators._price_utils import validate_prices, to_wide, to_long

logger = structlog.get_logger(__name__)

_WINDOW = 252
_MIN_PERIODS = 126


def compute_rolling_zscore_252d_raw_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Raw 252-day time-series z-score of price vs own history. Not cross-sectionally z-scored.

    More negative values indicate price is further below its annual mean
    (more oversold vs own history).
    """
    validate_prices(prices)
    wide = to_wide(prices)
    mean = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    std = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).std()
    z_raw = (wide - mean) / std.where(std > 0)
    result = to_long(z_raw, "rolling_zscore_252d_raw")
    logger.info(
        "rolling_zscore_252d_raw_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
    )
    return result

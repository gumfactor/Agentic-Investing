"""20-day Detrended Price Oscillator (DPO) factor.

DPO = Close - SMA(N/2 + 1) shifted back (N/2 + 1) periods.
Removes the long-term trend from price to isolate shorter cycles.
Positive = price above the detrended midpoint = short-cycle upswing.
Normalised by price for cross-sectional comparability.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_sma

logger = structlog.get_logger(__name__)

_WINDOW = 20
_HALF = _WINDOW // 2 + 1  # = 11


def compute_dpo_20_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of DPO(20) / price. Positive = short-cycle upswing."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = compute_sma(wide, window=_HALF)
    dpo = (wide - sma.shift(_HALF)) / wide
    z = cross_sectional_zscore(dpo)
    result = to_long(z, "dpo_20_score")
    logger.info("dpo_20_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

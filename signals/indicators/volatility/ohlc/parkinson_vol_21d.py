"""21-day Parkinson volatility factor.

Uses only the daily high/low range (no close-to-close gap).
Parkinson estimator = sqrt(1/(4·N·ln2) × Σ ln(H/L)²), annualized.
More efficient than close-to-close vol when overnight gaps are small.
Higher = more intraday price range = more volatile.
Requires ohlc DataFrame with columns [date, ticker, open, high, low, close].
Sign convention: use negative strategy weight for low-vol preference.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import to_long, cross_sectional_zscore
from signals.indicators._ohlc_utils import validate_ohlc, ohlc_wide

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 14
_FACTOR = 1.0 / (4.0 * np.log(2))


def compute_parkinson_vol_21d_scores(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day Parkinson vol. Higher = more intraday range volatility."""
    validate_ohlc(ohlc)
    high = ohlc_wide(ohlc, "high")
    low = ohlc_wide(ohlc, "low")
    log_hl_sq = np.log(high / low) ** 2
    park = (log_hl_sq.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean() * _FACTOR).pow(0.5) * np.sqrt(252)
    z = cross_sectional_zscore(park)
    result = to_long(z, "parkinson_vol_21d_score")
    logger.info("parkinson_vol_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

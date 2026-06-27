"""21-day Garman-Klass volatility factor.

Uses O/H/L/C — the most efficient single-period OHLC estimator.
GK_daily = 0.5·ln(H/L)² − (2·ln2 − 1)·ln(C/O)²
Annualized vol = sqrt(mean(GK_daily) × 252).
Higher = more volatile.
Requires ohlc DataFrame with columns [date, ticker, open, high, low, close].
Sign convention: use negative strategy weight for low-vol preference.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._ohlc_utils import validate_ohlc, ohlc_wide

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 14
_COEFF = 2.0 * np.log(2) - 1.0


def compute_garman_klass_vol_21d_scores(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 21-day Garman-Klass vol. Higher = more OHLC-measured volatility."""
    validate_ohlc(ohlc)
    high = ohlc_wide(ohlc, "high")
    low = ohlc_wide(ohlc, "low")
    open_ = ohlc_wide(ohlc, "open")
    close = ohlc_wide(ohlc, "close")
    gk_daily = 0.5 * np.log(high / low) ** 2 - _COEFF * np.log(close / open_) ** 2
    gk_vol = (gk_daily.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean() * 252).pow(0.5)
    z = cross_sectional_zscore(gk_vol)
    result = to_long(z, "garman_klass_vol_21d_score")
    logger.info("garman_klass_vol_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

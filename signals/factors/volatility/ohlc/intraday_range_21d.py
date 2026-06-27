"""21-day average intraday range factor.

Mean of (High − Low) / Close over the past 21 days.
Simpler than ATR: measures average daily bar width as a fraction of price.
Higher = wider daily bars = more intraday volatility.
Requires ohlc DataFrame with columns [date, ticker, open, high, low, close].
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import to_long, cross_sectional_zscore
from signals.factors._ohlc_utils import validate_ohlc, ohlc_wide

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 14


def compute_intraday_range_21d_scores(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of mean 21-day (H-L)/close. Higher = wider average daily bar."""
    validate_ohlc(ohlc)
    high = ohlc_wide(ohlc, "high")
    low = ohlc_wide(ohlc, "low")
    close = ohlc_wide(ohlc, "close")
    daily_range = (high - low) / close.where(close > 0)
    avg_range = daily_range.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    z = cross_sectional_zscore(avg_range)
    result = to_long(z, "intraday_range_21d_score")
    logger.info("intraday_range_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

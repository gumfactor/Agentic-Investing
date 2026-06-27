"""20-day CCI (Commodity Channel Index) factor.

CCI = (Price - SMA(20)) / (0.015 × MAD(20))
where MAD = mean absolute deviation of price from its 20-day mean.
Positive CCI = price above its typical value; high CCI = strong upward deviation.
Higher score = price significantly above its 20-day average = momentum signal.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 20
_MIN_PERIODS = 14
_CONSTANT = 0.015


def compute_cci_20_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of CCI(20). Higher = price more extended above 20-day mean."""
    validate_prices(prices)
    wide = to_wide(prices)
    sma = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).mean()
    mad = wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    cci = (wide - sma) / (_CONSTANT * mad.where(mad > 0))
    z = cross_sectional_zscore(cci)
    result = to_long(z, "cci_20_score")
    logger.info("cci_20_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

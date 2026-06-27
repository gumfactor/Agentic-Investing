"""Volatility-adjusted 12-month momentum factor.

12-month momentum (standard skip-1-month) divided by 252-day annualized
realized volatility. Stocks with strong momentum AND low volatility score
highest; high-vol momentum stocks are penalised.
Higher = more momentum per unit of risk.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)


def compute_vol_adjusted_mom_12m_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of mom_12m / realized_vol_252d. Higher = more risk-adjusted momentum."""
    validate_prices(prices)
    wide = to_wide(prices)
    daily_ret = wide.pct_change()
    mom = price_return(wide, lookback=252, skip=21)
    vol = daily_ret.rolling(252, min_periods=126).std() * np.sqrt(252)
    score = mom / vol.where(vol > 0)
    z = cross_sectional_zscore(score)
    result = to_long(z, "vol_adjusted_mom_12m_score")
    logger.info("vol_adjusted_mom_12m_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

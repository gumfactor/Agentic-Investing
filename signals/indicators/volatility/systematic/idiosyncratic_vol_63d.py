"""63-day idiosyncratic volatility factor.

Annualized standard deviation of the residual after removing the market
(SPY) return component using a rolling 63-day beta.
Captures firm-specific uncertainty not explained by market moves.
Higher = more company-specific risk.
Requires SPY to be present in the prices DataFrame.
Sign convention: use negative weight for low-idio-vol strategy.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, daily_return
from signals.indicators._market_utils import rolling_beta

logger = structlog.get_logger(__name__)

_WINDOW = 63
_MIN_PERIODS = 63  # full window (BUG-010): a gap anywhere in the trailing 63 returns suppresses the value
_BENCHMARK = "SPY"


def compute_idiosyncratic_vol_63d_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 63-day idiosyncratic vol. Higher = more firm-specific risk."""
    validate_prices(prices)
    wide = to_wide(prices)
    if _BENCHMARK not in wide.columns:
        raise ValueError("idiosyncratic_vol_63d requires 'SPY' to be present in prices")
    daily_ret = daily_return(wide)
    beta = rolling_beta(daily_ret, _BENCHMARK, _WINDOW, _MIN_PERIODS)
    spy_ret = daily_ret[_BENCHMARK]
    market_contribution = beta.multiply(spy_ret, axis=0)
    residuals = daily_ret.drop(columns=[_BENCHMARK]) - market_contribution.drop(columns=[_BENCHMARK])
    idio_vol = residuals.rolling(_WINDOW, min_periods=_MIN_PERIODS).std() * np.sqrt(252)
    z = cross_sectional_zscore(idio_vol)
    result = to_long(z, "idiosyncratic_vol_63d_score")
    logger.info("idiosyncratic_vol_63d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

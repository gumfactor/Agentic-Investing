"""12-month relative strength vs SPY factor.

Stock return over 252 days minus SPY return over the same period, skipping
the most recent month (standard momentum skip to avoid reversal contamination).
Positive = outperformed the market over the past year.
Higher score = stronger relative momentum vs the broad market.

Requires prices DataFrame to include a 'SPY' ticker row.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, price_return

logger = structlog.get_logger(__name__)

_LOOKBACK = 252
_SKIP = 21


def compute_rel_strength_vs_spy_12m_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of 12-month excess return vs SPY. Requires SPY in prices."""
    validate_prices(prices)
    wide = to_wide(prices)
    if "SPY" not in wide.columns:
        raise ValueError("rel_strength_vs_spy_12m requires 'SPY' to be present in prices")
    raw_ret = price_return(wide, lookback=_LOOKBACK, skip=_SKIP)
    spy_ret = raw_ret[["SPY"]]
    excess = raw_ret.subtract(spy_ret.values, axis=0)
    excess = excess.drop(columns=["SPY"], errors="ignore")
    z = cross_sectional_zscore(excess)
    result = to_long(z, "rel_strength_vs_spy_12m_score")
    logger.info("rel_strength_vs_spy_12m_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

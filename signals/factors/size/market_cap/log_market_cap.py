"""Log market cap factor (negated).

−ln(Price × Shares_Outstanding).
The primary Fama-French SMB size signal. Market cap is right-skewed across
any large universe, so log-transforming before z-scoring produces a
well-behaved cross-sectional distribution. Negated so that smaller firms
score higher, consistent with the historical small-cap return premium.
Higher = smaller market cap = stronger small-cap tilt.

Requires fundamentals column: shares_outstanding
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)


def compute_log_market_cap_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −ln(Market_Cap). Higher = smaller firm."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"shares_outstanding"})
    price_wide = to_wide(prices)
    shares = align_fundamentals(fund_to_wide(fundamentals, "shares_outstanding"), price_wide.index)
    market_cap = price_wide * shares
    log_mc = np.log(market_cap.where(market_cap > 0))
    z = cross_sectional_zscore(-log_mc)
    result = to_long(z, "log_market_cap_score")
    logger.info("log_market_cap_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

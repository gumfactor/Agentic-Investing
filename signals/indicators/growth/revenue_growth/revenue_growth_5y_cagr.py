"""Revenue 5-year CAGR factor.

(Revenue_TTM / Revenue_TTM_lag5Y)^(1/5) − 1.
Long-horizon top-line growth signal. Companies that consistently grow revenue
over a full market cycle demonstrate durable competitive advantages.
Requires ~1260 trading days of history; newer listings excluded as NaN.
Higher = stronger long-run compound revenue growth.

Requires fundamentals column: revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_5Y = 1260


def compute_revenue_growth_5y_cagr_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of 5Y revenue CAGR. Higher = stronger long-run top-line compounding."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm"})
    price_wide = to_wide(prices)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    lag = revenue.shift(_LAG_5Y)
    ratio = revenue / lag.where(lag > 0)
    cagr = ratio.where(ratio > 0) ** (1 / 5) - 1
    z = cross_sectional_zscore(cagr)
    result = to_long(z, "revenue_growth_5y_cagr_score")
    logger.info("revenue_growth_5y_cagr_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

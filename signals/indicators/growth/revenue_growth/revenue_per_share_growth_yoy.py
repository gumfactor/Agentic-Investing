"""Revenue per share year-over-year growth factor.

(Revenue_TTM / Shares − lag1Y) / lag1Y.
Dilution-adjusted top-line growth: a company can grow headline revenue
while simultaneously diluting equity holders via share issuance. Revenue
per share penalises that. Particularly relevant for high-growth companies
that frequently raise equity capital.
Higher = faster per-share top-line growth (dilution-adjusted).

Requires fundamentals columns: revenue_ttm, shares_outstanding
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)

_LAG_1Y = 252


def compute_revenue_per_share_growth_yoy_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of YoY revenue-per-share growth. Higher = dilution-adjusted top-line growth."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"revenue_ttm", "shares_outstanding"})
    price_wide = to_wide(prices)
    revenue = align_fundamentals(fund_to_wide(fundamentals, "revenue_ttm"), price_wide.index)
    shares = align_fundamentals(fund_to_wide(fundamentals, "shares_outstanding"), price_wide.index)
    rps = revenue / shares.where(shares > 0)
    lag = rps.shift(_LAG_1Y)
    growth = (rps - lag) / lag.where(lag > 0)
    z = cross_sectional_zscore(growth)
    result = to_long(z, "revenue_per_share_growth_yoy_score")
    logger.info("revenue_per_share_growth_yoy_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

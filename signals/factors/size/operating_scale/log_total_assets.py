"""Log total assets factor (negated).

−ln(Total_Assets).
Balance sheet size proxy. Unlike market cap it doesn't move daily with price,
making it a lower-noise size signal for rebalancing frequencies of monthly or
longer. Particularly useful for financials and real-estate, where asset scale
is the primary measure of business size.
Higher = smaller asset base = stronger small-firm tilt.

Requires fundamentals column: total_assets
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import validate_fundamentals, fund_to_wide, align_fundamentals

logger = structlog.get_logger(__name__)


def compute_log_total_assets_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −ln(Total_Assets). Higher = smaller balance sheet footprint."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"total_assets"})
    price_wide = to_wide(prices)
    assets = align_fundamentals(fund_to_wide(fundamentals, "total_assets"), price_wide.index)
    log_assets = np.log(assets.where(assets > 0))
    z = cross_sectional_zscore(-log_assets)
    result = to_long(z, "log_total_assets_score")
    logger.info("log_total_assets_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""Return on Invested Capital factor.

NOPAT_TTM / Invested_Capital.
The purest profitability signal: measures returns on capital actually deployed
in operations, stripping out the financing decision. Requires the data pipeline
to pre-compute NOPAT (EBIT × (1 − effective tax rate)) and Invested Capital
(Total Assets − Non-Interest-Bearing Current Liabilities − Excess Cash).
Higher = more after-tax operating profit per dollar of invested capital.

Requires fundamentals columns: nopat_ttm, invested_capital
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_roic_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of NOPAT_TTM / Invested_Capital. Higher = better capital efficiency."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"nopat_ttm", "invested_capital"})
    price_wide = to_wide(prices)
    nopat = align_fundamentals(fund_to_wide(fundamentals, "nopat_ttm"), price_wide.index)
    ic = align_fundamentals(fund_to_wide(fundamentals, "invested_capital"), price_wide.index)
    ratio = nopat / ic.where(ic > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "roic_score")
    logger.info("roic_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

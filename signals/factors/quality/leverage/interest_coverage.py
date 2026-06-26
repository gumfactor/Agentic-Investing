"""Interest coverage ratio factor.

EBIT_TTM / Interest_Expense_TTM.
How many times over a company can cover its interest obligations from
operating income. Companies with zero interest expense (no debt) are
excluded as NaN — they are effectively unconstrained but rare in large-cap
universes. Capped at 50× to limit outlier influence on the z-score.
Higher = more comfortable debt service capacity.

Requires fundamentals columns: ebit_ttm, interest_expense_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)

_CAP = 50.0


def compute_interest_coverage_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of EBIT / Interest_Expense (capped at 50×). Higher = safer debt load."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"ebit_ttm", "interest_expense_ttm"})
    price_wide = to_wide(prices)
    ebit = align_fundamentals(fund_to_wide(fundamentals, "ebit_ttm"), price_wide.index)
    interest = align_fundamentals(fund_to_wide(fundamentals, "interest_expense_ttm"), price_wide.index)
    ratio = (ebit / interest.where(interest > 0)).clip(upper=_CAP)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "interest_coverage_score")
    logger.info("interest_coverage_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

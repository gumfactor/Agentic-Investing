"""Cash earnings ratio factor.

Operating_CF_TTM / Net_Income_TTM.
When operating cash flow substantially exceeds net income, earnings are
well-supported by actual cash generation. Ratios below 1 suggest accrual-heavy
reporting; ratios well above 1 suggest conservative accounting.
Only defined for companies with positive net income (loss firms excluded).
Higher = more cash behind each dollar of reported profit.

Requires fundamentals columns: operating_cf_ttm, net_income_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_cash_earnings_ratio_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of OCF / Net_Income (positive-NI firms only). Higher = cash quality."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"operating_cf_ttm", "net_income_ttm"})
    price_wide = to_wide(prices)
    ocf = align_fundamentals(fund_to_wide(fundamentals, "operating_cf_ttm"), price_wide.index)
    net_income = align_fundamentals(fund_to_wide(fundamentals, "net_income_ttm"), price_wide.index)
    ratio = ocf / net_income.where(net_income > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "cash_earnings_ratio_score")
    logger.info("cash_earnings_ratio_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

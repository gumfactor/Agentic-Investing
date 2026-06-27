"""Log enterprise value factor (negated).

−ln(EV)  where  EV = Price × Shares + Total_Debt − Cash.
Debt-inclusive size measure. A company with a small market cap but heavy
debt load is not truly small — EV captures the full capital burden.
Particularly relevant in leveraged sectors (utilities, REITs, industrials)
where market-cap-only size signals can be misleading.
Only defined where EV > 0 (net-cash companies with EV ≤ 0 excluded).
Higher = smaller enterprise value = stronger small-cap tilt on a debt-adjusted basis.

Requires fundamentals columns: shares_outstanding, total_debt, cash
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, compute_ev_wide,
)

logger = structlog.get_logger(__name__)

_EV_COLS = {"shares_outstanding", "total_debt", "cash"}


def compute_log_enterprise_value_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of −ln(EV). Higher = smaller debt-adjusted firm size."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, _EV_COLS)
    price_wide = to_wide(prices)
    ev = compute_ev_wide(price_wide, fundamentals)
    log_ev = np.log(ev.where(ev > 0))
    z = cross_sectional_zscore(-log_ev)
    result = to_long(z, "log_enterprise_value_score")
    logger.info("log_enterprise_value_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

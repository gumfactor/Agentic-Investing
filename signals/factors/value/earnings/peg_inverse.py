"""Inverse PEG ratio factor (Growth at a Reasonable Price).

PEG = (P/E) / EPS_growth_rate
PEG_inverse = EPS_growth_rate × (EPS_TTM / Price) = earnings_yield × growth_rate.
Higher = stock is cheap AND growing fast (best GARP candidates).
Negative values occur when earnings yield is negative (loss-making) or
when growth is negative (shrinking earnings) — both rank poorly, correctly.

Requires fundamentals columns: eps_ttm, eps_growth_rate
  eps_growth_rate: forward or trailing annualized EPS growth as a decimal
                   (e.g. 0.15 = 15% growth). Computed in the data pipeline.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_peg_inverse_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of earnings_yield × eps_growth_rate. Higher = cheap and growing."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"eps_ttm", "eps_growth_rate"})
    price_wide = to_wide(prices)
    eps = align_fundamentals(fund_to_wide(fundamentals, "eps_ttm"), price_wide.index)
    growth = align_fundamentals(fund_to_wide(fundamentals, "eps_growth_rate"), price_wide.index)
    earnings_yield = eps / price_wide.where(price_wide > 0)
    peg_inv = earnings_yield * growth
    z = cross_sectional_zscore(peg_inv)
    result = to_long(z, "peg_inverse_score")
    logger.info("peg_inverse_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

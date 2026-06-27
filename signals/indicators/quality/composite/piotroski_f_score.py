"""Piotroski F-Score factor (Piotroski 2000).

Sum of 9 binary quality signals (0–9). Each signal scores 1 if the condition
is met, 0 otherwise (missing data treated conservatively as 0).

Profitability (4 signals):
  F1  ROA > 0
  F2  Operating cash flow > 0
  F3  ROA improved YoY
  F4  Operating CF / Assets > ROA (cash-backed earnings)

Leverage, Liquidity & Dilution (3 signals):
  F5  Long-term debt / Assets decreased YoY
  F6  Current ratio improved YoY
  F7  Shares outstanding did not increase YoY (no dilution)

Operating Efficiency (2 signals):
  F8  Gross margin improved YoY
  F9  Asset turnover improved YoY

YoY change approximated by a 252-trading-day shift on the daily-aligned series.
Higher total score = stronger overall financial condition.

Requires fundamentals columns:
  net_income_ttm, operating_cf_ttm, total_assets, long_term_debt,
  current_assets, current_liabilities, shares_outstanding,
  gross_profit_ttm, revenue_ttm
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.indicators._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)

_YEAR_LAG = 252

_REQUIRED_COLS = {
    "net_income_ttm", "operating_cf_ttm", "total_assets", "long_term_debt",
    "current_assets", "current_liabilities", "shares_outstanding",
    "gross_profit_ttm", "revenue_ttm",
}


def _flag(cond: pd.DataFrame) -> pd.DataFrame:
    """True → 1.0, False/NaN → 0.0 (conservative: missing data earns no credit)."""
    return cond.astype(float)


def compute_piotroski_f_score_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of the 9-signal Piotroski F-Score. Higher = stronger financials."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, _REQUIRED_COLS)
    price_wide = to_wide(prices)
    idx = price_wide.index

    def _align(col: str) -> pd.DataFrame:
        return align_fundamentals(fund_to_wide(fundamentals, col), idx)

    net_income = _align("net_income_ttm")
    ocf = _align("operating_cf_ttm")
    assets = _align("total_assets")
    lt_debt = _align("long_term_debt")
    cur_assets = _align("current_assets")
    cur_liab = _align("current_liabilities")
    shares = _align("shares_outstanding")
    gross_profit = _align("gross_profit_ttm")
    revenue = _align("revenue_ttm")

    roa = net_income / assets.where(assets > 0)
    ocf_ratio = ocf / assets.where(assets > 0)
    leverage = lt_debt / assets.where(assets > 0)
    cur_ratio = cur_assets / cur_liab.where(cur_liab > 0)
    gross_margin = gross_profit / revenue.where(revenue > 0)
    asset_turn = revenue / assets.where(assets > 0)

    f1 = _flag(roa > 0)
    f2 = _flag(ocf > 0)
    f3 = _flag(roa > roa.shift(_YEAR_LAG))
    f4 = _flag(ocf_ratio > roa)
    f5 = _flag(leverage < leverage.shift(_YEAR_LAG))
    f6 = _flag(cur_ratio > cur_ratio.shift(_YEAR_LAG))
    f7 = _flag(shares <= shares.shift(_YEAR_LAG))
    f8 = _flag(gross_margin > gross_margin.shift(_YEAR_LAG))
    f9 = _flag(asset_turn > asset_turn.shift(_YEAR_LAG))

    f_score = f1 + f2 + f3 + f4 + f5 + f6 + f7 + f8 + f9
    z = cross_sectional_zscore(f_score)
    result = to_long(z, "piotroski_f_score_score")
    logger.info("piotroski_f_score_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result

"""Unit tests for signals/factors/quality/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.indicators.quality.profitability.roe import compute_roe_scores
from signals.indicators.quality.profitability.roa import compute_roa_scores
from signals.indicators.quality.profitability.roic import compute_roic_scores
from signals.indicators.quality.profitability.roce import compute_roce_scores
from signals.indicators.quality.profitability.gross_margin import compute_gross_margin_scores
from signals.indicators.quality.profitability.operating_margin import compute_operating_margin_scores
from signals.indicators.quality.profitability.net_margin import compute_net_margin_scores
from signals.indicators.quality.earnings_quality.sloan_accrual import compute_sloan_accrual_scores
from signals.indicators.quality.earnings_quality.cash_earnings_ratio import compute_cash_earnings_ratio_scores
from signals.indicators.quality.earnings_quality.balance_sheet_accrual import compute_balance_sheet_accrual_scores
from signals.indicators.quality.leverage.interest_coverage import compute_interest_coverage_scores
from signals.indicators.quality.leverage.current_ratio import compute_current_ratio_scores
from signals.indicators.quality.leverage.quick_ratio import compute_quick_ratio_scores
from signals.indicators.quality.leverage.net_debt_to_ebitda import compute_net_debt_to_ebitda_scores
from signals.indicators.quality.leverage.debt_to_equity import compute_debt_to_equity_scores
from signals.indicators.quality.capital_efficiency.asset_turnover import compute_asset_turnover_scores
from signals.indicators.quality.capital_efficiency.inventory_turnover import compute_inventory_turnover_scores
from signals.indicators.quality.capital_efficiency.receivables_turnover import compute_receivables_turnover_scores
from signals.indicators.quality.earnings_stability.eps_stability import compute_eps_stability_scores
from signals.indicators.quality.earnings_stability.revenue_stability import compute_revenue_stability_scores
from signals.indicators.quality.earnings_stability.earnings_consistency import compute_earnings_consistency_scores
from signals.indicators.quality.composite.piotroski_f_score import compute_piotroski_f_score_scores

from signals.tests.factors.conftest import make_fixed_prices, make_fundamentals, _latest_scores

# Piotroski requires 9 columns; use tuple specs so cross-sectional variation produces valid z-scores.
_PIOTROSKI_KWARGS = dict(
    net_income_ttm=(100.0, 20.0),
    operating_cf_ttm=(120.0, 25.0),
    total_assets=(1000.0, 100.0),
    long_term_debt=(200.0, 40.0),
    current_assets=(300.0, 50.0),
    current_liabilities=(150.0, 30.0),
    shares_outstanding=(500.0, 50.0),
    gross_profit_ttm=(400.0, 60.0),
    revenue_ttm=(800.0, 80.0),
)

# ─── Smoke tests: all 22 quality factors ──────────────────────────────────────

# balance_sheet_accrual and piotroski use shift(252), so they need prices_400d to
# produce valid (non-NaN) cross-sectional scores.
_QUALITY_CASES_300D = [
    (compute_roe_scores, {"net_income_ttm": (100.0, 20.0), "shareholders_equity": (500.0, 80.0)}, "roe_score"),
    (compute_roa_scores, {"net_income_ttm": (100.0, 20.0), "total_assets": (1000.0, 100.0)}, "roa_score"),
    (compute_roic_scores, {"nopat_ttm": (80.0, 15.0), "invested_capital": (600.0, 80.0)}, "roic_score"),
    (compute_roce_scores, {"ebit_ttm": (120.0, 20.0), "total_assets": (1000.0, 100.0), "current_liabilities": (150.0, 30.0)}, "roce_score"),
    (compute_gross_margin_scores, {"gross_profit_ttm": (400.0, 60.0), "revenue_ttm": (800.0, 80.0)}, "gross_margin_score"),
    (compute_operating_margin_scores, {"ebit_ttm": (150.0, 25.0), "revenue_ttm": (800.0, 80.0)}, "operating_margin_score"),
    (compute_net_margin_scores, {"net_income_ttm": (100.0, 20.0), "revenue_ttm": (800.0, 80.0)}, "net_margin_score"),
    (compute_sloan_accrual_scores, {"net_income_ttm": (100.0, 20.0), "operating_cf_ttm": (120.0, 25.0), "total_assets": (1000.0, 100.0)}, "sloan_accrual_score"),
    (compute_cash_earnings_ratio_scores, {"operating_cf_ttm": (120.0, 25.0), "net_income_ttm": (100.0, 20.0)}, "cash_earnings_ratio_score"),
    (compute_interest_coverage_scores, {"ebit_ttm": (150.0, 25.0), "interest_expense_ttm": (20.0, 5.0)}, "interest_coverage_score"),
    (compute_current_ratio_scores, {"current_assets": (300.0, 50.0), "current_liabilities": (150.0, 30.0)}, "current_ratio_score"),
    (compute_quick_ratio_scores, {"current_assets": (300.0, 50.0), "inventory": (80.0, 15.0), "current_liabilities": (150.0, 30.0)}, "quick_ratio_score"),
    (compute_net_debt_to_ebitda_scores, {"total_debt": (300.0, 50.0), "cash": (50.0, 10.0), "ebitda_ttm": (200.0, 30.0)}, "net_debt_to_ebitda_score"),
    (compute_debt_to_equity_scores, {"total_debt": (300.0, 50.0), "shareholders_equity": (500.0, 80.0)}, "debt_to_equity_score"),
    (compute_asset_turnover_scores, {"revenue_ttm": (800.0, 80.0), "total_assets": (1000.0, 100.0)}, "asset_turnover_score"),
    (compute_inventory_turnover_scores, {"cogs_ttm": (400.0, 60.0), "inventory": (80.0, 15.0)}, "inventory_turnover_score"),
    (compute_receivables_turnover_scores, {"revenue_ttm": (800.0, 80.0), "accounts_receivable": (100.0, 20.0)}, "receivables_turnover_score"),
    (compute_eps_stability_scores, {"eps_ttm": (5.0, 2.0)}, "eps_stability_score"),
    (compute_revenue_stability_scores, {"revenue_ttm": (800.0, 200.0)}, "revenue_stability_score"),
    # Wide std ensures mix of positive/negative quarters so consistency varies cross-sectionally.
    (compute_earnings_consistency_scores, {"eps_ttm": (1.0, 4.0)}, "earnings_consistency_score"),
]

# These need prices_400d (shift-252 factors require enough window for valid z-scores)
_QUALITY_CASES_400D = [
    (compute_balance_sheet_accrual_scores,
     {"net_operating_assets": (600.0, 80.0), "total_assets": (1000.0, 100.0)},
     "balance_sheet_accrual_score"),
    (compute_piotroski_f_score_scores, _PIOTROSKI_KWARGS, "piotroski_f_score_score"),
]


@pytest.mark.parametrize("fn,fund_kwargs,score_col", _QUALITY_CASES_300D, ids=[c[2] for c in _QUALITY_CASES_300D])
def test_quality_factor_smoke_300d(fn, fund_kwargs, score_col, prices_300d):
    """Every quality factor runs without error and produces correct output schema."""
    fund = make_fundamentals(n_quarters=16, **fund_kwargs)
    result = fn(prices_300d, fund)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,fund_kwargs,score_col", _QUALITY_CASES_400D, ids=[c[2] for c in _QUALITY_CASES_400D])
def test_quality_factor_smoke_400d(fn, fund_kwargs, score_col, prices_400d):
    """Shift-252 quality factors produce valid scores when fundamentals span the price window.

    start="2020-01-01" ensures quarterly dates fall within prices_400d so the 252-day
    YoY shift compares two distinct quarterly values rather than a constant forward-fill.
    """
    fund = make_fundamentals(n_quarters=8, start="2020-01-01", **fund_kwargs)
    result = fn(prices_400d, fund)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


# ─── Z-score properties ────────────────────────────────────────────────────────

def test_roe_zscores_cross_sectionally_normalized(prices_300d):
    """Cross-sectional z-scores are mean 0 and sample std 1 at the latest date."""
    fund = make_fundamentals(net_income_ttm=(100.0, 20.0), shareholders_equity=(500.0, 80.0))
    result = compute_roe_scores(prices_300d, fund)
    scores = _latest_scores(result, "roe_score").dropna()
    assert len(scores) >= 3
    assert abs(scores.mean()) < 1e-10
    assert abs(scores.std() - 1.0) < 1e-10


# ─── Sign direction tests ──────────────────────────────────────────────────────

def test_roe_higher_net_income_same_equity_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"],
                              net_income_ttm=[200.0, 50.0],
                              shareholders_equity=500.0)
    result = compute_roe_scores(prices, fund)
    scores = _latest_scores(result, "roe_score")
    assert scores["HI"] > scores["LO"]


def test_roa_higher_net_income_same_assets_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"],
                              net_income_ttm=[300.0, 50.0],
                              total_assets=1000.0)
    result = compute_roa_scores(prices, fund)
    scores = _latest_scores(result, "roa_score")
    assert scores["HI"] > scores["LO"]


def test_sloan_accrual_cash_backed_earnings_scores_higher():
    """Higher OCF relative to NI (lower accrual) → higher sloan_accrual score (negated)."""
    prices = make_fixed_prices(["CASH", "ACCRUAL"], close=100.0)
    # CASH: OCF ≈ NI (low accrual). ACCRUAL: OCF << NI (high accrual)
    fund = make_fundamentals(["CASH", "ACCRUAL"],
                              net_income_ttm=100.0,
                              operating_cf_ttm=[100.0, 20.0],
                              total_assets=1000.0)
    result = compute_sloan_accrual_scores(prices, fund)
    scores = _latest_scores(result, "sloan_accrual_score")
    assert scores["CASH"] > scores["ACCRUAL"]


def test_interest_coverage_more_ebit_scores_higher():
    """Higher EBIT relative to same interest expense → higher coverage → higher score."""
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"],
                              ebit_ttm=[500.0, 30.0],
                              interest_expense_ttm=50.0)
    result = compute_interest_coverage_scores(prices, fund)
    scores = _latest_scores(result, "interest_coverage_score")
    assert scores["HI"] > scores["LO"]


def test_interest_coverage_capped_at_50x():
    """Coverage ratios > 50× are clipped; two tickers both above the cap score identically."""
    # Three tickers required so cross-sectional std > 0 (two identical values would be NaN).
    prices = make_fixed_prices(["HUGE", "ABOVE_CAP", "BELOW_CAP"], close=100.0)
    # HUGE: 200× (capped). ABOVE_CAP: 60× (capped). BELOW_CAP: 5×
    fund = make_fundamentals(["HUGE", "ABOVE_CAP", "BELOW_CAP"],
                              ebit_ttm=[200.0, 60.0, 5.0],
                              interest_expense_ttm=1.0)
    result = compute_interest_coverage_scores(prices, fund)
    scores = _latest_scores(result, "interest_coverage_score")
    # Both above-cap tickers clip to 50× → same raw ratio → same z-score
    assert abs(scores["HUGE"] - scores["ABOVE_CAP"]) < 1e-10
    # Below-cap ticker has lower coverage → lower score
    assert scores["HUGE"] > scores["BELOW_CAP"]


def test_interest_coverage_zero_interest_ticker_excluded():
    """Tickers with interest_expense ≤ 0 are excluded by interest.where(interest > 0).

    Three tickers: two with positive interest (retained), one with zero interest
    (excluded). Confirms the guard clause drives exclusion, not single-ticker std=NaN.
    """
    prices = make_fixed_prices(["POS1", "POS2", "ZERO_INT"], close=100.0)
    fund = make_fundamentals(["POS1", "POS2", "ZERO_INT"],
                              ebit_ttm=[100.0, 150.0, 200.0],
                              interest_expense_ttm=[10.0, 20.0, 0.0])
    result = compute_interest_coverage_scores(prices, fund)
    tickers = set(result["ticker"].unique())
    assert "POS1" in tickers
    assert "POS2" in tickers
    assert "ZERO_INT" not in tickers


def test_net_debt_to_ebitda_lower_leverage_scores_higher():
    """Lower net debt relative to EBITDA → lower raw ratio → higher score (negated)."""
    prices = make_fixed_prices(["LOW", "HIGH"], close=100.0)
    fund = make_fundamentals(["LOW", "HIGH"],
                              total_debt=[100.0, 900.0],
                              cash=50.0,
                              ebitda_ttm=200.0)
    result = compute_net_debt_to_ebitda_scores(prices, fund)
    scores = _latest_scores(result, "net_debt_to_ebitda_score")
    assert scores["LOW"] > scores["HIGH"]


def test_debt_to_equity_less_debt_scores_higher():
    """Lower debt / equity → higher score (negated factor)."""
    prices = make_fixed_prices(["LOW", "HIGH"], close=100.0)
    fund = make_fundamentals(["LOW", "HIGH"],
                              total_debt=[50.0, 800.0],
                              shareholders_equity=500.0)
    result = compute_debt_to_equity_scores(prices, fund)
    scores = _latest_scores(result, "debt_to_equity_score")
    assert scores["LOW"] > scores["HIGH"]


def test_earnings_consistency_always_positive_scores_higher():
    """A ticker with consistently positive EPS scores higher than one with mixed quarters."""
    prices = make_fixed_prices(["CONS", "INCONS"], close=100.0)
    # CONS: eps always positive. INCONS: eps alternates negative/positive
    fund_cons = make_fundamentals(["CONS"], n_quarters=16, eps_ttm=5.0)
    # Alternating sign — 8 positive, 8 negative across quarters
    import numpy as np
    dates = fund_cons[fund_cons["ticker"] == "CONS"]["date"].values
    n = len(dates)
    eps_vals = [5.0 if i % 2 == 0 else -5.0 for i in range(n)]
    rows_incons = [{"date": d, "ticker": "INCONS", "eps_ttm": v}
                   for d, v in zip(dates, eps_vals)]
    import pandas as pd_inner
    fund_incons = pd_inner.DataFrame(rows_incons)
    fund = pd_inner.concat([fund_cons, fund_incons])
    result = compute_earnings_consistency_scores(prices, fund)
    scores = _latest_scores(result, "earnings_consistency_score")
    assert scores["CONS"] > scores["INCONS"]


def test_gross_margin_higher_margin_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"],
                              gross_profit_ttm=[700.0, 200.0],
                              revenue_ttm=800.0)
    result = compute_gross_margin_scores(prices, fund)
    scores = _latest_scores(result, "gross_margin_score")
    assert scores["HI"] > scores["LO"]


# ─── Validation ───────────────────────────────────────────────────────────────

def test_roe_missing_shareholders_equity_raises(prices_300d):
    fund = make_fundamentals(net_income_ttm=100.0)
    with pytest.raises(ValueError, match="missing required metric columns"):
        compute_roe_scores(prices_300d, fund)


def test_piotroski_missing_column_raises(prices_300d):
    """Missing any of the 9 required Piotroski columns raises ValueError."""
    fund = make_fundamentals(net_income_ttm=100.0, operating_cf_ttm=120.0)
    with pytest.raises(ValueError, match="missing required metric columns"):
        compute_piotroski_f_score_scores(prices_300d, fund)


# ─── Guard tests ──────────────────────────────────────────────────────────────

def test_roe_negative_equity_excluded_from_score():
    """Negative equity (equity.where(equity > 0) → NaN) is excluded from the ratio.

    Two positive-equity tickers are needed so cross-sectional std > 0 and valid z-scores exist.
    """
    prices = make_fixed_prices(["POS1", "POS2", "NEG"], close=100.0)
    fund = make_fundamentals(["POS1", "POS2", "NEG"],
                              net_income_ttm=100.0,
                              shareholders_equity=[500.0, 300.0, -200.0])
    result = compute_roe_scores(prices, fund)
    tickers_in_result = set(_latest_scores(result, "roe_score").index)
    assert "POS1" in tickers_in_result
    assert "POS2" in tickers_in_result
    assert "NEG" not in tickers_in_result


def test_cash_earnings_ratio_loss_firms_excluded():
    """Negative net income (net_income.where(>0) → NaN) → ratio NaN → dropped from result."""
    prices = make_fixed_prices(["PROFIT1", "PROFIT2", "LOSS"], close=100.0)
    fund = make_fundamentals(["PROFIT1", "PROFIT2", "LOSS"],
                              operating_cf_ttm=50.0,
                              net_income_ttm=[100.0, 80.0, -50.0])
    result = compute_cash_earnings_ratio_scores(prices, fund)
    tickers_in_result = set(_latest_scores(result, "cash_earnings_ratio_score").index)
    assert "PROFIT1" in tickers_in_result
    assert "PROFIT2" in tickers_in_result
    assert "LOSS" not in tickers_in_result

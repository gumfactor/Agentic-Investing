"""Unit tests for signals/indicators/growth/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.indicators.growth.earnings_growth.eps_growth_yoy import compute_eps_growth_yoy_scores
from signals.indicators.growth.earnings_growth.eps_growth_3y_cagr import compute_eps_growth_3y_cagr_scores
from signals.indicators.growth.earnings_growth.eps_growth_5y_cagr import compute_eps_growth_5y_cagr_scores
from signals.indicators.growth.earnings_growth.eps_growth_acceleration import compute_eps_growth_acceleration_scores
from signals.indicators.growth.revenue_growth.revenue_growth_yoy import compute_revenue_growth_yoy_scores
from signals.indicators.growth.revenue_growth.revenue_growth_3y_cagr import compute_revenue_growth_3y_cagr_scores
from signals.indicators.growth.revenue_growth.revenue_growth_5y_cagr import compute_revenue_growth_5y_cagr_scores
from signals.indicators.growth.revenue_growth.revenue_per_share_growth_yoy import compute_revenue_per_share_growth_yoy_scores
from signals.indicators.growth.revenue_growth.ebitda_growth_yoy import compute_ebitda_growth_yoy_scores
from signals.indicators.growth.cash_flow_growth.fcf_growth_yoy import compute_fcf_growth_yoy_scores
from signals.indicators.growth.cash_flow_growth.ocf_growth_yoy import compute_ocf_growth_yoy_scores
from signals.indicators.growth.cash_flow_growth.fcf_growth_3y_cagr import compute_fcf_growth_3y_cagr_scores
from signals.indicators.growth.cash_flow_growth.ocf_growth_3y_cagr import compute_ocf_growth_3y_cagr_scores
from signals.indicators.growth.margin_expansion.gross_margin_expansion_yoy import compute_gross_margin_expansion_yoy_scores
from signals.indicators.growth.margin_expansion.operating_margin_expansion_yoy import compute_operating_margin_expansion_yoy_scores
from signals.indicators.growth.margin_expansion.net_margin_expansion_yoy import compute_net_margin_expansion_yoy_scores
from signals.indicators.growth.profitability_improvement.roa_improvement_yoy import compute_roa_improvement_yoy_scores
from signals.indicators.growth.profitability_improvement.roic_improvement_yoy import compute_roic_improvement_yoy_scores
from signals.indicators.growth.profitability_improvement.roe_improvement_yoy import compute_roe_improvement_yoy_scores
from signals.indicators.growth.book_value_growth.book_value_per_share_growth_yoy import compute_book_value_per_share_growth_yoy_scores

from signals.tests.indicators.conftest import make_prices, make_fixed_prices, make_fundamentals, _latest_scores

# ─── Smoke tests ──────────────────────────────────────────────────────────────

# YoY factors (shift-252): need prices_400d so the last 148 days have valid scores.
_GROWTH_YOY_CASES = [
    (compute_eps_growth_yoy_scores, {"eps_ttm": (5.0, 1.0)}, "eps_growth_yoy_score"),
    (compute_eps_growth_acceleration_scores, {"eps_ttm": (5.0, 1.0)}, "eps_growth_acceleration_score"),
    (compute_revenue_growth_yoy_scores, {"revenue_ttm": (500.0, 50.0)}, "revenue_growth_yoy_score"),
    (compute_revenue_per_share_growth_yoy_scores, {"revenue_ttm": (500.0, 50.0), "shares_outstanding": (200.0, 20.0)}, "revenue_per_share_growth_yoy_score"),
    (compute_ebitda_growth_yoy_scores, {"ebitda_ttm": (200.0, 30.0)}, "ebitda_growth_yoy_score"),
    (compute_fcf_growth_yoy_scores, {"fcf_ttm": (150.0, 25.0)}, "fcf_growth_yoy_score"),
    (compute_ocf_growth_yoy_scores, {"operating_cf_ttm": (180.0, 30.0)}, "ocf_growth_yoy_score"),
    (compute_gross_margin_expansion_yoy_scores, {"gross_profit_ttm": (400.0, 60.0), "revenue_ttm": (800.0, 80.0)}, "gross_margin_expansion_yoy_score"),
    (compute_operating_margin_expansion_yoy_scores, {"ebit_ttm": (150.0, 25.0), "revenue_ttm": (800.0, 80.0)}, "operating_margin_expansion_yoy_score"),
    (compute_net_margin_expansion_yoy_scores, {"net_income_ttm": (100.0, 20.0), "revenue_ttm": (800.0, 80.0)}, "net_margin_expansion_yoy_score"),
    (compute_roa_improvement_yoy_scores, {"net_income_ttm": (100.0, 20.0), "total_assets": (1000.0, 100.0)}, "roa_improvement_yoy_score"),
    (compute_roic_improvement_yoy_scores, {"nopat_ttm": (80.0, 15.0), "invested_capital": (600.0, 80.0)}, "roic_improvement_yoy_score"),
    (compute_roe_improvement_yoy_scores, {"net_income_ttm": (100.0, 20.0), "shareholders_equity": (500.0, 80.0)}, "roe_improvement_yoy_score"),
    (compute_book_value_per_share_growth_yoy_scores, {"shareholders_equity": (500.0, 80.0), "shares_outstanding": (200.0, 20.0)}, "book_value_per_share_growth_yoy_score"),
]

# 3Y CAGR (shift-756): need prices_810d so the last ~54 days have valid scores.
_GROWTH_3Y_CASES = [
    (compute_eps_growth_3y_cagr_scores, {"eps_ttm": (5.0, 1.0)}, "eps_growth_3y_cagr_score"),
    (compute_revenue_growth_3y_cagr_scores, {"revenue_ttm": (500.0, 50.0)}, "revenue_growth_3y_cagr_score"),
    (compute_fcf_growth_3y_cagr_scores, {"fcf_ttm": (150.0, 25.0)}, "fcf_growth_3y_cagr_score"),
    (compute_ocf_growth_3y_cagr_scores, {"operating_cf_ttm": (180.0, 30.0)}, "ocf_growth_3y_cagr_score"),
]

# 5Y CAGR (shift-1260): need ≥ 1300 days; created locally (no fixture covers this).
_GROWTH_5Y_CASES = [
    (compute_eps_growth_5y_cagr_scores, {"eps_ttm": (5.0, 1.0)}, "eps_growth_5y_cagr_score"),
    (compute_revenue_growth_5y_cagr_scores, {"revenue_ttm": (500.0, 50.0)}, "revenue_growth_5y_cagr_score"),
]


@pytest.mark.parametrize("fn,fund_kwargs,score_col", _GROWTH_YOY_CASES, ids=[c[2] for c in _GROWTH_YOY_CASES])
def test_growth_yoy_factor_smoke(fn, fund_kwargs, score_col, prices_400d):
    """YoY growth factors run without error and produce valid scores.

    start="2020-01-01" places quarterly dates inside the price window so the
    shift(252) comparison sees two distinct quarterly values, not a constant.
    """
    fund = make_fundamentals(n_quarters=8, start="2020-01-01", **fund_kwargs)
    result = fn(prices_400d, fund)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,fund_kwargs,score_col", _GROWTH_3Y_CASES, ids=[c[2] for c in _GROWTH_3Y_CASES])
def test_growth_3y_cagr_factor_smoke(fn, fund_kwargs, score_col, prices_810d):
    """3Y CAGR factors produce valid scores; 16 quarters from 2020-01-01 span the 810-day window."""
    fund = make_fundamentals(n_quarters=16, start="2020-01-01", **fund_kwargs)
    result = fn(prices_810d, fund)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,fund_kwargs,score_col", _GROWTH_5Y_CASES, ids=[c[2] for c in _GROWTH_5Y_CASES])
def test_growth_5y_cagr_factor_smoke(fn, fund_kwargs, score_col):
    """5Y CAGR factors produce valid scores; 24 quarters from 2020-01-01 span 1400 trading days."""
    prices = make_prices(n_days=1400, start="2020-01-01")
    fund = make_fundamentals(n_quarters=24, start="2020-01-01", **fund_kwargs)
    result = fn(prices, fund)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


# ─── Z-score properties ───────────────────────────────────────────────────────

def test_eps_growth_yoy_zscores_cross_sectionally_normalized():
    """At the latest date, cross-sectional mean is 0 and sample std is 1."""
    prices = make_prices(n_days=700, start="2020-01-01")
    fund = make_fundamentals(n_quarters=8, start="2020-01-01", eps_ttm=(5.0, 1.0))
    result = compute_eps_growth_yoy_scores(prices, fund)
    scores = _latest_scores(result, "eps_growth_yoy_score").dropna()
    assert len(scores) >= 3
    assert abs(scores.mean()) < 1e-10
    assert abs(scores.std() - 1.0) < 1e-10


# ─── Sign direction tests ──────────────────────────────────────────────────────
# These use a two-point fundamental: early value (pre-2022) held constant for all
# tickers, then a late value (from 2022 onward) that differs by ticker. With 700
# price days from 2020-01-01, the test date (~Oct 2022) sees late values while
# shift(252) (~Oct 2021) sees early values, giving meaningful YoY growth rates.

def _yoy_fund(tickers, col, early_val, late_vals):
    """Two-point fundamentals for YoY growth sign tests."""
    rows = []
    for ticker, late_val in zip(tickers, late_vals):
        rows.append({"date": pd.Timestamp("2016-01-01"), "ticker": ticker, col: float(early_val)})
        rows.append({"date": pd.Timestamp("2022-01-01"), "ticker": ticker, col: float(late_val)})
    return pd.DataFrame(rows)


def test_eps_growth_yoy_faster_growth_scores_higher():
    """Ticker whose EPS doubled scores higher than one whose EPS barely grew."""
    prices = make_prices(["HI", "LO"], n_days=700, start="2020-01-01")
    fund = _yoy_fund(["HI", "LO"], "eps_ttm", early_val=2.0, late_vals=[4.0, 2.2])
    result = compute_eps_growth_yoy_scores(prices, fund)
    scores = _latest_scores(result, "eps_growth_yoy_score")
    assert scores["HI"] > scores["LO"]


def test_revenue_growth_yoy_faster_growth_scores_higher():
    prices = make_prices(["HI", "LO"], n_days=700, start="2020-01-01")
    fund = _yoy_fund(["HI", "LO"], "revenue_ttm", early_val=500.0, late_vals=[1000.0, 520.0])
    result = compute_revenue_growth_yoy_scores(prices, fund)
    scores = _latest_scores(result, "revenue_growth_yoy_score")
    assert scores["HI"] > scores["LO"]


def test_gross_margin_expansion_expanding_company_scores_higher():
    """Ticker whose gross margin improved YoY scores higher than one that compressed."""
    tickers = ["EXP", "COMP"]
    prices = make_fixed_prices(tickers, close=100.0, n_days=700)
    # Build two-point fundamentals for both gross_profit_ttm and revenue_ttm.
    # EXP: gross margin 40% → 60%. COMP: gross margin 40% → 30%.
    rows = []
    early = {"gross_profit_ttm": 400.0, "revenue_ttm": 1000.0}   # 40% margin
    late = {"EXP": {"gross_profit_ttm": 600.0, "revenue_ttm": 1000.0},  # 60% margin
            "COMP": {"gross_profit_ttm": 300.0, "revenue_ttm": 1000.0}}  # 30% margin
    for t in tickers:
        rows.append({"date": pd.Timestamp("2016-01-01"), "ticker": t, **early})
        rows.append({"date": pd.Timestamp("2022-01-01"), "ticker": t, **late[t]})
    fund = pd.DataFrame(rows)
    result = compute_gross_margin_expansion_yoy_scores(prices, fund)
    scores = _latest_scores(result, "gross_margin_expansion_yoy_score")
    assert scores["EXP"] > scores["COMP"]


def test_fcf_growth_yoy_faster_growth_scores_higher():
    prices = make_prices(["HI", "LO"], n_days=700, start="2020-01-01")
    fund = _yoy_fund(["HI", "LO"], "fcf_ttm", early_val=100.0, late_vals=[300.0, 110.0])
    result = compute_fcf_growth_yoy_scores(prices, fund)
    scores = _latest_scores(result, "fcf_growth_yoy_score")
    assert scores["HI"] > scores["LO"]


def test_roa_improvement_yoy_improving_company_scores_higher():
    """Ticker with rising ROA YoY outscores ticker with flat ROA."""
    tickers = ["UP", "FLAT"]
    prices = make_fixed_prices(tickers, close=100.0, n_days=700)
    rows = []
    for t in tickers:
        early_ni = 50.0
        late_ni = 150.0 if t == "UP" else 50.0
        rows.append({"date": pd.Timestamp("2016-01-01"), "ticker": t,
                     "net_income_ttm": early_ni, "total_assets": 1000.0})
        rows.append({"date": pd.Timestamp("2022-01-01"), "ticker": t,
                     "net_income_ttm": late_ni, "total_assets": 1000.0})
    fund = pd.DataFrame(rows)
    result = compute_roa_improvement_yoy_scores(prices, fund)
    scores = _latest_scores(result, "roa_improvement_yoy_score")
    assert scores["UP"] > scores["FLAT"]


# ─── Guard tests ──────────────────────────────────────────────────────────────

def test_eps_growth_yoy_negative_base_excluded():
    """When base-year EPS is negative, YoY growth is undefined (NaN) — not a crash.

    Three tickers: two valid (POS_BASE, POS_BASE2) so cross-sectional std ≠ 0,
    one excluded (NEG_BASE) because its lagged EPS is negative.
    700-day price window from 2020-01-01 (ends ~Oct 2022) ensures the 2022-01-01
    late date is inside the window so shift(252) compares two distinct values.
    """
    prices = make_fixed_prices(["NEG_BASE", "POS_BASE", "POS_BASE2"], close=100.0, n_days=700)
    rows = []
    bases = {"NEG_BASE": -2.0, "POS_BASE": 2.0, "POS_BASE2": 3.0}
    lates = {"NEG_BASE": 4.0, "POS_BASE": 4.0, "POS_BASE2": 9.0}
    for t in ["NEG_BASE", "POS_BASE", "POS_BASE2"]:
        rows.append({"date": pd.Timestamp("2016-01-01"), "ticker": t, "eps_ttm": bases[t]})
        rows.append({"date": pd.Timestamp("2022-01-01"), "ticker": t, "eps_ttm": lates[t]})
    fund = pd.DataFrame(rows)
    result = compute_eps_growth_yoy_scores(prices, fund)
    # NEG_BASE has lag < 0 → NaN growth → NaN z-score → dropped
    latest_tickers = set(_latest_scores(result, "eps_growth_yoy_score").index)
    assert "POS_BASE" in latest_tickers
    assert "POS_BASE2" in latest_tickers
    assert "NEG_BASE" not in latest_tickers


def test_cagr_negative_ratio_produces_nan_not_crash():
    """CAGR with ratio.where(ratio > 0) — negative current value → NaN, not complex number.

    Three tickers: two valid (GROW, GROW2) so cross-sectional std ≠ 0,
    one excluded (TURN) because its current EPS turned negative.
    Asserts valid tickers retain real (non-NaN) scores to confirm the guard
    clause is responsible for TURN's exclusion, not a total scoring failure.
    """
    prices = make_fixed_prices(["TURN", "GROW", "GROW2"], close=100.0, n_days=810)
    rows = []
    lates = {"TURN": -1.0, "GROW": 8.0, "GROW2": 6.0}
    for t in ["TURN", "GROW", "GROW2"]:
        rows.append({"date": pd.Timestamp("2016-01-01"), "ticker": t, "eps_ttm": 2.0})
        rows.append({"date": pd.Timestamp("2022-01-01"), "ticker": t, "eps_ttm": lates[t]})
    fund = pd.DataFrame(rows)
    result = compute_eps_growth_3y_cagr_scores(prices, fund)
    # TURN: ratio < 0 → NaN after .where(ratio > 0) → dropped
    latest = _latest_scores(result, "eps_growth_3y_cagr_score")
    assert "GROW" in latest.index
    assert "GROW2" in latest.index
    assert "TURN" not in latest.index
    assert latest[["GROW", "GROW2"]].notna().all()


# ─── Validation ───────────────────────────────────────────────────────────────

def test_eps_growth_yoy_missing_eps_ttm_raises(prices_400d):
    fund = make_fundamentals(revenue_ttm=500.0)
    with pytest.raises(ValueError, match="missing required metric columns"):
        compute_eps_growth_yoy_scores(prices_400d, fund)


def test_gross_margin_expansion_missing_revenue_raises(prices_400d):
    fund = make_fundamentals(gross_profit_ttm=400.0)
    with pytest.raises(ValueError, match="missing required metric columns"):
        compute_gross_margin_expansion_yoy_scores(prices_400d, fund)

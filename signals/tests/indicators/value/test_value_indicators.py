"""Unit tests for signals/indicators/value/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.indicators.value.earnings.earnings_yield_ttm import compute_earnings_yield_ttm_scores
from signals.indicators.value.earnings.earnings_yield_normalized import compute_earnings_yield_normalized_scores
from signals.indicators.value.earnings.forward_earnings_yield import compute_forward_earnings_yield_scores
from signals.indicators.value.earnings.peg_inverse import compute_peg_inverse_scores
from signals.indicators.value.book_value.book_to_market import compute_book_to_market_scores
from signals.indicators.value.book_value.tangible_book_to_price import compute_tangible_book_to_price_scores
from signals.indicators.value.sales.sales_to_price import compute_sales_to_price_scores
from signals.indicators.value.sales.ev_to_sales_inverse import compute_ev_to_sales_inverse_scores
from signals.indicators.value.cash_flow.fcf_yield import compute_fcf_yield_scores
from signals.indicators.value.cash_flow.operating_cf_yield import compute_operating_cf_yield_scores
from signals.indicators.value.cash_flow.ev_to_fcf_inverse import compute_ev_to_fcf_inverse_scores
from signals.indicators.value.ev_multiples.ebitda_to_ev import compute_ebitda_to_ev_scores
from signals.indicators.value.ev_multiples.ebit_to_ev import compute_ebit_to_ev_scores
from signals.indicators.value.shareholder_yield.dividend_yield import compute_dividend_yield_scores
from signals.indicators.value.shareholder_yield.buyback_yield import compute_buyback_yield_scores
from signals.indicators.value.shareholder_yield.shareholder_yield import compute_shareholder_yield_scores

from signals.tests.indicators.conftest import make_fixed_prices, make_fundamentals, _latest_scores

# EV baseline: realistic positive-EV balance sheet used by EV-based factors
_EV = dict(shares_outstanding=(1000.0, 50.0), total_debt=(200.0, 20.0), cash=(50.0, 5.0))

# ─── Smoke tests: all 16 value factors ────────────────────────────────────────

_VALUE_CASES = [
    (compute_earnings_yield_ttm_scores, {"eps_ttm": (5.0, 1.0)}, "earnings_yield_ttm_score"),
    (compute_earnings_yield_normalized_scores, {"eps_normalized": (3.0, 0.5)}, "earnings_yield_normalized_score"),
    (compute_forward_earnings_yield_scores, {"eps_forward": (4.0, 0.8)}, "forward_earnings_yield_score"),
    (compute_peg_inverse_scores, {"eps_ttm": (5.0, 1.0), "eps_growth_rate": (0.15, 0.05)}, "peg_inverse_score"),
    (compute_book_to_market_scores, {"book_value_per_share": (20.0, 4.0)}, "book_to_market_score"),
    (compute_tangible_book_to_price_scores, {"tangible_book_value_per_share": (15.0, 3.0)}, "tangible_book_to_price_score"),
    (compute_sales_to_price_scores, {"revenue_per_share": (50.0, 10.0)}, "sales_to_price_score"),
    (compute_ev_to_sales_inverse_scores, {"revenue_ttm": (500.0, 50.0), **_EV}, "ev_to_sales_inverse_score"),
    (compute_fcf_yield_scores, {"fcf_per_share": (3.0, 0.5)}, "fcf_yield_score"),
    (compute_operating_cf_yield_scores, {"operating_cf_per_share": (4.0, 0.5)}, "operating_cf_yield_score"),
    (compute_ev_to_fcf_inverse_scores, {"fcf_ttm": (200.0, 30.0), **_EV}, "ev_to_fcf_inverse_score"),
    (compute_ebitda_to_ev_scores, {"ebitda_ttm": (300.0, 40.0), **_EV}, "ebitda_to_ev_score"),
    (compute_ebit_to_ev_scores, {"ebit_ttm": (250.0, 35.0), **_EV}, "ebit_to_ev_score"),
    (compute_dividend_yield_scores, {"dividends_per_share": (2.0, 0.3)}, "dividend_yield_score"),
    (compute_buyback_yield_scores, {"net_buybacks_per_share": (1.5, 0.3)}, "buyback_yield_score"),
    (compute_shareholder_yield_scores,
     {"dividends_per_share": (2.0, 0.3), "net_buybacks_per_share": (1.5, 0.3)},
     "shareholder_yield_score"),
]


@pytest.mark.parametrize("fn,fund_kwargs,score_col", _VALUE_CASES, ids=[c[2] for c in _VALUE_CASES])
def test_value_factor_smoke(fn, fund_kwargs, score_col, prices_300d):
    """Every value indicator runs without error and produces correct output schema."""
    fund = make_fundamentals(n_quarters=16, **fund_kwargs)
    result = fn(prices_300d, fund)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


# ─── Z-score properties ────────────────────────────────────────────────────────

def test_earnings_yield_ttm_zscores_cross_sectionally_normalized(prices_300d):
    """At the latest date, cross-sectional mean is 0 and sample std is 1 (by construction)."""
    fund = make_fundamentals(eps_ttm=(5.0, 1.0))
    result = compute_earnings_yield_ttm_scores(prices_300d, fund)
    scores = _latest_scores(result, "earnings_yield_ttm_score").dropna()
    assert len(scores) >= 3
    assert abs(scores.mean()) < 1e-10
    assert abs(scores.std() - 1.0) < 1e-10


# ─── Sign direction tests ──────────────────────────────────────────────────────

def test_earnings_yield_higher_eps_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"], eps_ttm=[10.0, 2.0])
    result = compute_earnings_yield_ttm_scores(prices, fund)
    scores = _latest_scores(result, "earnings_yield_ttm_score")
    assert scores["HI"] > scores["LO"]


def test_book_to_market_higher_bvps_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"], book_value_per_share=[80.0, 20.0])
    result = compute_book_to_market_scores(prices, fund)
    scores = _latest_scores(result, "book_to_market_score")
    assert scores["HI"] > scores["LO"]


def test_sales_to_price_higher_revenue_per_share_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"], revenue_per_share=[50.0, 10.0])
    result = compute_sales_to_price_scores(prices, fund)
    scores = _latest_scores(result, "sales_to_price_score")
    assert scores["HI"] > scores["LO"]


def test_fcf_yield_higher_fcf_per_share_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"], fcf_per_share=[8.0, 2.0])
    result = compute_fcf_yield_scores(prices, fund)
    scores = _latest_scores(result, "fcf_yield_score")
    assert scores["HI"] > scores["LO"]


def test_dividend_yield_higher_dps_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"], dividends_per_share=[5.0, 1.0])
    result = compute_dividend_yield_scores(prices, fund)
    scores = _latest_scores(result, "dividend_yield_score")
    assert scores["HI"] > scores["LO"]


def test_ebitda_to_ev_higher_ebitda_same_balance_sheet_scores_higher():
    """More EBITDA relative to the same EV → higher EBITDA/EV → higher score."""
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"],
                              ebitda_ttm=[1000.0, 200.0],
                              shares_outstanding=100.0,
                              total_debt=500.0,
                              cash=100.0)
    result = compute_ebitda_to_ev_scores(prices, fund)
    scores = _latest_scores(result, "ebitda_to_ev_score")
    assert scores["HI"] > scores["LO"]


def test_ebit_to_ev_higher_ebit_same_balance_sheet_scores_higher():
    prices = make_fixed_prices(["HI", "LO"], close=100.0)
    fund = make_fundamentals(["HI", "LO"],
                              ebit_ttm=[800.0, 100.0],
                              shares_outstanding=100.0,
                              total_debt=500.0,
                              cash=100.0)
    result = compute_ebit_to_ev_scores(prices, fund)
    scores = _latest_scores(result, "ebit_to_ev_score")
    assert scores["HI"] > scores["LO"]


def test_peg_inverse_high_growth_and_cheap_scores_highest():
    """Ticker with both high earnings yield and high growth rate scores highest."""
    prices = make_fixed_prices(["GARP", "CHEAP", "GROW"], close=100.0)
    # GARP: high EPS + high growth; CHEAP: high EPS low growth; GROW: low EPS high growth
    fund = make_fundamentals(["GARP", "CHEAP", "GROW"],
                              eps_ttm=[10.0, 10.0, 2.0],
                              eps_growth_rate=[0.3, 0.05, 0.3])
    result = compute_peg_inverse_scores(prices, fund)
    scores = _latest_scores(result, "peg_inverse_score")
    assert scores["GARP"] > scores["CHEAP"]
    assert scores["GARP"] > scores["GROW"]


# ─── Validation ───────────────────────────────────────────────────────────────

def test_earnings_yield_ttm_missing_eps_ttm_raises(prices_300d):
    fund = make_fundamentals(revenue_ttm=100.0)
    with pytest.raises(ValueError, match="missing required metric columns"):
        compute_earnings_yield_ttm_scores(prices_300d, fund)


def test_ebitda_to_ev_missing_shares_outstanding_raises(prices_300d):
    fund = make_fundamentals(ebitda_ttm=300.0, total_debt=100.0, cash=50.0)
    with pytest.raises(ValueError, match="missing required metric columns"):
        compute_ebitda_to_ev_scores(prices_300d, fund)


def test_value_factor_empty_prices_raises():
    empty = pd.DataFrame(columns=["date", "ticker", "close"])
    fund = make_fundamentals(eps_ttm=5.0)
    with pytest.raises(ValueError):
        compute_earnings_yield_ttm_scores(empty, fund)


# ─── Guard tests ──────────────────────────────────────────────────────────────

def test_ev_factors_negative_ev_ticker_excluded():
    """Net-cash company (EV ≤ 0) is excluded; two positive-EV peers are retained.

    Three tickers: two with different positive EVs (so std > 0), one excluded.
    NEG_EV: EV = 1*1 + 0 − 10000 < 0 → excluded.
    """
    # NEG_EV: close=$1, shares=1, debt=0, cash=10000 → EV ≈ -9999 → excluded
    # SM_EV:  close=$10, shares=10, debt=0, cash=0   → EV = 100 → kept
    # LG_EV:  close=$100, shares=100, debt=200, cash=50 → EV = 10150 → kept
    prices = pd.concat([
        make_fixed_prices(["NEG_EV"], close=1.0, n_days=50),
        make_fixed_prices(["SM_EV"], close=10.0, n_days=50),
        make_fixed_prices(["LG_EV"], close=100.0, n_days=50),
    ])
    fund = make_fundamentals(["NEG_EV", "SM_EV", "LG_EV"],
                              ebitda_ttm=[100.0, 20.0, 1000.0],
                              shares_outstanding=[1.0, 10.0, 100.0],
                              total_debt=[0.0, 0.0, 200.0],
                              cash=[10000.0, 0.0, 50.0])
    result = compute_ebitda_to_ev_scores(prices, fund)
    assert {"date", "ticker", "ebitda_to_ev_score"} <= set(result.columns)
    tickers = set(result["ticker"].unique())
    assert "SM_EV" in tickers
    assert "LG_EV" in tickers
    assert "NEG_EV" not in tickers


def test_earnings_yield_negative_eps_valid_scores_lower():
    """Negative EPS is valid — the indicator just returns a low (negative) ratio, not NaN."""
    prices = make_fixed_prices(["POS", "NEG"], close=100.0)
    fund = make_fundamentals(["POS", "NEG"], eps_ttm=[5.0, -3.0])
    result = compute_earnings_yield_ttm_scores(prices, fund)
    scores = _latest_scores(result, "earnings_yield_ttm_score")
    assert scores["POS"] > scores["NEG"]
    assert scores["NEG"] < 0  # negative yield → below-mean z-score


def test_book_to_market_negative_book_value_scores_lower():
    """Negative book value per share is valid and yields a negative B/M ratio → lower score."""
    prices = make_fixed_prices(["POS", "NEG"], close=100.0)
    fund = make_fundamentals(["POS", "NEG"], book_value_per_share=[50.0, -10.0])
    result = compute_book_to_market_scores(prices, fund)
    scores = _latest_scores(result, "book_to_market_score")
    assert scores["POS"] > scores["NEG"]

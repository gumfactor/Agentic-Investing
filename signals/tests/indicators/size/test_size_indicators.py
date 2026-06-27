"""Unit tests for signals/factors/size/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.indicators.size.market_cap.log_market_cap import compute_log_market_cap_scores
from signals.indicators.size.market_cap.log_enterprise_value import compute_log_enterprise_value_scores
from signals.indicators.size.operating_scale.log_total_assets import compute_log_total_assets_scores
from signals.indicators.size.operating_scale.log_revenue_ttm import compute_log_revenue_ttm_scores

from signals.tests.factors.conftest import make_fixed_prices, make_fundamentals, _latest_scores

# ─── Smoke tests: all 4 size factors ──────────────────────────────────────────

_SIZE_CASES = [
    (compute_log_market_cap_scores,
     {"shares_outstanding": (1000.0, 200.0)},
     "log_market_cap_score"),
    (compute_log_enterprise_value_scores,
     {"shares_outstanding": (1000.0, 200.0), "total_debt": (200.0, 30.0), "cash": (50.0, 10.0)},
     "log_enterprise_value_score"),
    (compute_log_total_assets_scores,
     {"total_assets": (5000.0, 800.0)},
     "log_total_assets_score"),
    (compute_log_revenue_ttm_scores,
     {"revenue_ttm": (2000.0, 400.0)},
     "log_revenue_ttm_score"),
]


@pytest.mark.parametrize("fn,fund_kwargs,score_col", _SIZE_CASES, ids=[c[2] for c in _SIZE_CASES])
def test_size_factor_smoke(fn, fund_kwargs, score_col, prices_300d):
    """Every size indicator runs without error and produces correct output schema."""
    fund = make_fundamentals(n_quarters=16, **fund_kwargs)
    result = fn(prices_300d, fund)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


# ─── Z-score properties ────────────────────────────────────────────────────────

def test_log_total_assets_zscores_cross_sectionally_normalized(prices_300d):
    """Cross-sectional z-scores are mean 0 and sample std 1 at the latest date."""
    fund = make_fundamentals(total_assets=(5000.0, 800.0))
    result = compute_log_total_assets_scores(prices_300d, fund)
    scores = _latest_scores(result, "log_total_assets_score").dropna()
    assert len(scores) >= 3
    assert abs(scores.mean()) < 1e-10
    assert abs(scores.std() - 1.0) < 1e-10


# ─── Sign direction tests (size is negated: smaller = higher score) ───────────

def test_log_market_cap_smaller_firm_scores_higher():
    """−ln(market_cap): smaller market cap → lower ln → less negative after negation → higher score."""
    prices = make_fixed_prices(["SMALL", "BIG"], close=100.0)
    fund = make_fundamentals(["SMALL", "BIG"], shares_outstanding=[100.0, 100_000.0])
    result = compute_log_market_cap_scores(prices, fund)
    scores = _latest_scores(result, "log_market_cap_score")
    assert scores["SMALL"] > scores["BIG"]


def test_log_enterprise_value_smaller_ev_scores_higher():
    """−ln(EV): firm with fewer shares (smaller EV, same debt/cash structure) scores higher."""
    prices = make_fixed_prices(["SMALL", "BIG"], close=100.0)
    fund = make_fundamentals(["SMALL", "BIG"],
                              shares_outstanding=[100.0, 100_000.0],
                              total_debt=200.0,
                              cash=50.0)
    result = compute_log_enterprise_value_scores(prices, fund)
    scores = _latest_scores(result, "log_enterprise_value_score")
    assert scores["SMALL"] > scores["BIG"]


def test_log_total_assets_smaller_balance_sheet_scores_higher():
    """−ln(total_assets): firm with a smaller asset base scores higher."""
    prices = make_fixed_prices(["SMALL", "BIG"], close=100.0)
    fund = make_fundamentals(["SMALL", "BIG"], total_assets=[500.0, 100_000.0])
    result = compute_log_total_assets_scores(prices, fund)
    scores = _latest_scores(result, "log_total_assets_score")
    assert scores["SMALL"] > scores["BIG"]


def test_log_revenue_ttm_smaller_revenue_scores_higher():
    """−ln(revenue): firm with less revenue (smaller operating scale) scores higher."""
    prices = make_fixed_prices(["SMALL", "BIG"], close=100.0)
    fund = make_fundamentals(["SMALL", "BIG"], revenue_ttm=[200.0, 50_000.0])
    result = compute_log_revenue_ttm_scores(prices, fund)
    scores = _latest_scores(result, "log_revenue_ttm_score")
    assert scores["SMALL"] > scores["BIG"]


# ─── Guard tests ──────────────────────────────────────────────────────────────

def test_log_enterprise_value_negative_ev_ticker_excluded():
    """Net-cash company (EV ≤ 0) is excluded; two positive-EV peers are retained.

    Three tickers: two with positive EV (different sizes, so std > 0), one excluded.
    """
    # NEG_EV: close=$1, shares=1, debt=0, cash=10000 → EV ≈ -9999 < 0 → excluded
    # SM_EV:  close=$10, shares=10, debt=0, cash=0   → EV = 100 → kept
    # LG_EV:  close=$100, shares=100, debt=200, cash=50 → EV = 10150 → kept
    prices = pd.concat([
        make_fixed_prices(["NEG_EV"], close=1.0, n_days=50),
        make_fixed_prices(["SM_EV"], close=10.0, n_days=50),
        make_fixed_prices(["LG_EV"], close=100.0, n_days=50),
    ])
    fund = make_fundamentals(["NEG_EV", "SM_EV", "LG_EV"],
                              shares_outstanding=[1.0, 10.0, 100.0],
                              total_debt=[0.0, 0.0, 200.0],
                              cash=[10000.0, 0.0, 50.0])
    result = compute_log_enterprise_value_scores(prices, fund)
    assert {"date", "ticker", "log_enterprise_value_score"} <= set(result.columns)
    tickers = set(result["ticker"].unique())
    assert "SM_EV" in tickers
    assert "LG_EV" in tickers
    assert "NEG_EV" not in tickers


def test_log_revenue_zero_revenue_ticker_excluded():
    """Zero-revenue ticker is excluded; two valid peers with different revenues are retained."""
    prices = make_fixed_prices(["ZERO", "SM", "LG"], close=100.0, n_days=50)
    fund = make_fundamentals(["ZERO", "SM", "LG"], revenue_ttm=[0.0, 200.0, 5000.0])
    result = compute_log_revenue_ttm_scores(prices, fund)
    tickers = set(result["ticker"].unique())
    assert "SM" in tickers
    assert "LG" in tickers
    assert "ZERO" not in tickers


def test_log_total_assets_zero_assets_ticker_excluded():
    """Zero-assets ticker is excluded; two valid peers with different asset bases are retained."""
    prices = make_fixed_prices(["ZERO", "SM", "LG"], close=100.0, n_days=50)
    fund = make_fundamentals(["ZERO", "SM", "LG"], total_assets=[0.0, 500.0, 100_000.0])
    result = compute_log_total_assets_scores(prices, fund)
    tickers = set(result["ticker"].unique())
    assert "SM" in tickers
    assert "LG" in tickers
    assert "ZERO" not in tickers


# ─── Validation ───────────────────────────────────────────────────────────────

def test_log_market_cap_missing_shares_outstanding_raises(prices_300d):
    fund = make_fundamentals(total_assets=1000.0)
    with pytest.raises(ValueError, match="missing required metric columns"):
        compute_log_market_cap_scores(prices_300d, fund)


def test_log_enterprise_value_missing_cash_raises(prices_300d):
    fund = make_fundamentals(shares_outstanding=1000.0, total_debt=200.0)
    with pytest.raises(ValueError, match="missing required metric columns"):
        compute_log_enterprise_value_scores(prices_300d, fund)


def test_log_revenue_empty_fundamentals_raises(prices_300d):
    fund = pd.DataFrame({"date": [], "ticker": [], "revenue_ttm": []})
    with pytest.raises(ValueError):
        compute_log_revenue_ttm_scores(prices_300d, fund)

"""Tests for signals/composites/garp.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.garp import compute_garp_scores


# ── Fixtures ──────────────────────────────────────────────────────────────────

TICKERS = ["A", "B", "C", "D", "E"]
DATES = [date(2024, 1, 31), date(2024, 2, 29)]


def _z(arr):
    a = np.array(arr, dtype=float)
    return list((a - a.mean()) / a.std(ddof=1))


def _make_scores(col: str, values_by_date: dict) -> pd.DataFrame:
    rows = []
    for d, vals in values_by_date.items():
        for ticker, v in zip(TICKERS, vals):
            rows.append({"ticker": ticker, "date": d, col: v})
    return pd.DataFrame(rows)


def _value_scores(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([1, 2, 3, 4, 5]) for d in DATES}
    return _make_scores("earnings_yield", values_by_date)


def _eps_scores(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([1, 2, 3, 4, 5]) for d in DATES}
    return _make_scores("eps_growth_3y_cagr_score", values_by_date)


def _rev_scores(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([1, 2, 3, 4, 5]) for d in DATES}
    return _make_scores("revenue_growth_3y_cagr_score", values_by_date)


# ── Basic output (2-signal) ───────────────────────────────────────────────────

def test_output_columns_no_revenue():
    result = compute_garp_scores(_value_scores(), _eps_scores())
    assert set(result.columns) >= {"ticker", "date", "earnings_yield",
                                    "eps_growth_3y_cagr_score", "garp_score"}
    assert "revenue_growth_3y_cagr_score" not in result.columns


def test_output_columns_with_revenue():
    result = compute_garp_scores(_value_scores(), _eps_scores(), _rev_scores())
    assert "revenue_growth_3y_cagr_score" in result.columns


def test_output_shape():
    result = compute_garp_scores(_value_scores(), _eps_scores())
    assert len(result) == len(TICKERS) * len(DATES)


def test_output_sorted():
    result = compute_garp_scores(_value_scores(), _eps_scores())
    assert list(result["date"]) == sorted(result["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    result = compute_garp_scores(_value_scores(), _eps_scores())
    for d in DATES:
        scores = result.loc[result["date"] == d, "garp_score"].dropna()
        assert abs(scores.mean()) < 1e-10
        assert abs(scores.std(ddof=1) - 1.0) < 1e-10


def test_cross_sectional_zscore_with_revenue():
    result = compute_garp_scores(_value_scores(), _eps_scores(), _rev_scores())
    for d in DATES:
        scores = result.loc[result["date"] == d, "garp_score"].dropna()
        assert abs(scores.mean()) < 1e-10
        assert abs(scores.std(ddof=1) - 1.0) < 1e-10


# ── Intuitive ordering ────────────────────────────────────────────────────────

def test_high_both_signals_highest():
    """Ticker with highest earnings yield AND highest EPS growth should top GARP."""
    vs = _make_scores("earnings_yield", {DATES[0]: _z([5, 4, 3, 2, 1])})
    eps = _make_scores("eps_growth_3y_cagr_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_garp_scores(vs, eps)
    top = result.loc[result["date"] == DATES[0]].nlargest(1, "garp_score")
    assert top.iloc[0]["ticker"] == "A"


def test_opposing_signals_cancel():
    """Highest earnings yield + lowest EPS growth should produce middling GARP (near zero)."""
    vs = _make_scores("earnings_yield", {DATES[0]: _z([5, 4, 3, 2, 1])})
    eps = _make_scores("eps_growth_3y_cagr_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    result = compute_garp_scores(vs, eps, earnings_yield_weight=0.5, eps_growth_weight=0.5)
    scores = result.loc[result["date"] == DATES[0]].set_index("ticker")["garp_score"]
    assert all(abs(scores) < 1e-10)


# ── Revenue growth optional leg ───────────────────────────────────────────────

def test_revenue_weight_redistributed_when_absent():
    """With and without revenue, the ranking of A vs E should be preserved when
    all three signals agree (high for A, low for E)."""
    vs = _make_scores("earnings_yield", {DATES[0]: _z([5, 4, 3, 2, 1])})
    eps = _make_scores("eps_growth_3y_cagr_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    rev = _make_scores("revenue_growth_3y_cagr_score", {DATES[0]: _z([5, 4, 3, 2, 1])})

    no_rev = compute_garp_scores(vs, eps)
    with_rev = compute_garp_scores(vs, eps, rev)

    for result in (no_rev, with_rev):
        d = result[result["date"] == DATES[0]]
        assert d.nlargest(1, "garp_score").iloc[0]["ticker"] == "A"
        assert d.nsmallest(1, "garp_score").iloc[0]["ticker"] == "E"


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_eps_falls_back_to_earnings_yield():
    """When EPS growth is NaN for a ticker, full weight goes to earnings_yield."""
    vs = _make_scores("earnings_yield", {DATES[0]: _z([5, 4, 3, 2, 1])})
    eps_rows = [
        {"ticker": "A", "date": DATES[0], "eps_growth_3y_cagr_score": np.nan},
        {"ticker": "B", "date": DATES[0], "eps_growth_3y_cagr_score": 1.0},
        {"ticker": "C", "date": DATES[0], "eps_growth_3y_cagr_score": 0.0},
        {"ticker": "D", "date": DATES[0], "eps_growth_3y_cagr_score": -1.0},
        {"ticker": "E", "date": DATES[0], "eps_growth_3y_cagr_score": -2.0},
    ]
    eps = pd.DataFrame(eps_rows)
    result = compute_garp_scores(vs, eps)
    a_row = result[(result["ticker"] == "A") & (result["date"] == DATES[0])]
    assert len(a_row) == 1
    assert not pd.isna(a_row.iloc[0]["garp_score"])


def test_both_missing_dropped():
    vs = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "earnings_yield": np.nan},
        {"ticker": "B", "date": DATES[0], "earnings_yield": 1.0},
    ])
    eps = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "eps_growth_3y_cagr_score": np.nan},
        {"ticker": "B", "date": DATES[0], "eps_growth_3y_cagr_score": 0.5},
    ])
    result = compute_garp_scores(vs, eps)
    assert "A" not in result["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    vs = pd.DataFrame([{"ticker": "A", "date": DATES[0], "earnings_yield": 1.0}])
    eps = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "eps_growth_3y_cagr_score": 0.5},
        {"ticker": "B", "date": DATES[0], "eps_growth_3y_cagr_score": -0.5},
    ])
    result = compute_garp_scores(vs, eps)
    assert "B" in result["ticker"].values
    b = result[(result["ticker"] == "B") & (result["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["garp_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_earnings_yield_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong_col": 1.0}])
    with pytest.raises(ValueError, match="value_scores missing"):
        compute_garp_scores(bad, _eps_scores())


def test_missing_eps_growth_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong_col": 1.0}])
    with pytest.raises(ValueError, match="eps_growth_scores missing"):
        compute_garp_scores(_value_scores(), bad)


def test_missing_revenue_growth_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong_col": 1.0}])
    with pytest.raises(ValueError, match="revenue_growth_scores missing"):
        compute_garp_scores(_value_scores(), _eps_scores(), bad)

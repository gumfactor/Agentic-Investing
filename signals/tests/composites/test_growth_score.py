"""Tests for signals/composites/growth_score.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.growth_score import compute_growth_scores


TICKERS = ["A", "B", "C", "D", "E"]
DATES = [date(2024, 1, 31), date(2024, 2, 29)]


def _z(arr):
    a = np.array(arr, dtype=float)
    return list((a - a.mean()) / a.std(ddof=1))


def _make(col, values_by_date):
    rows = []
    for d, vals in values_by_date.items():
        for ticker, v in zip(TICKERS, vals):
            rows.append({"ticker": ticker, "date": d, col: v})
    return pd.DataFrame(rows)


def _rev_growth(vbd=None):
    return _make("revenue_growth_3y_cagr_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _eps_growth(vbd=None):
    return _make("eps_growth_3y_cagr_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _fcf_growth(vbd=None):
    return _make("fcf_growth_3y_cagr_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _margin_exp(vbd=None):
    return _make("operating_margin_expansion_yoy_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _eps_accel(vbd=None):
    return _make("eps_growth_acceleration_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _all_inputs(**kwargs):
    return (
        kwargs.get("rev", _rev_growth()),
        kwargs.get("eps", _eps_growth()),
        kwargs.get("fcf", _fcf_growth()),
        kwargs.get("margin", _margin_exp()),
        kwargs.get("accel", _eps_accel()),
    )


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_growth_scores(*_all_inputs())
    assert set(r.columns) >= {
        "ticker", "date",
        "revenue_growth_3y_cagr_score", "eps_growth_3y_cagr_score",
        "fcf_growth_3y_cagr_score", "operating_margin_expansion_yoy_score",
        "eps_growth_acceleration_score", "growth_score",
    }


def test_output_shape():
    r = compute_growth_scores(*_all_inputs())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_growth_scores(*_all_inputs())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_growth_scores(*_all_inputs())
    for d in DATES:
        s = r.loc[r["date"] == d, "growth_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Directional correctness ───────────────────────────────────────────────────

def test_best_on_all_dimensions_tops():
    """A ticker scoring highest on every dimension should have the top composite."""
    vals_best = {DATES[0]: _z([5, 4, 3, 2, 1])}   # A = best
    r = compute_growth_scores(
        _rev_growth(vals_best), _eps_growth(vals_best),
        _fcf_growth(vals_best), _margin_exp(vals_best), _eps_accel(vals_best),
    )
    top = r[r["date"] == DATES[0]].nlargest(1, "growth_score")
    assert top.iloc[0]["ticker"] == "A"


def test_worst_on_all_dimensions_bottom():
    """A ticker scoring lowest on every dimension should have the bottom composite."""
    vals = {DATES[0]: _z([5, 4, 3, 2, 1])}   # E = worst
    r = compute_growth_scores(
        _rev_growth(vals), _eps_growth(vals),
        _fcf_growth(vals), _margin_exp(vals), _eps_accel(vals),
    )
    bot = r[r["date"] == DATES[0]].nsmallest(1, "growth_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_pure_revenue_weight():
    """With only revenue_growth_weight non-zero, rank == revenue growth rank."""
    rev = _rev_growth({DATES[0]: _z([1, 2, 3, 4, 5])})
    other = {DATES[0]: _z([5, 4, 3, 2, 1])}   # oppose: would favour E on other dims
    r = compute_growth_scores(
        rev,
        _eps_growth(other), _fcf_growth(other), _margin_exp(other), _eps_accel(other),
        revenue_growth_weight=1.0,
        eps_growth_weight=0.0,
        fcf_growth_weight=0.0,
        margin_expansion_weight=0.0,
        eps_acceleration_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("growth_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


def test_pure_eps_weight():
    """With only eps_growth_weight non-zero, rank == EPS growth rank."""
    eps = _eps_growth({DATES[0]: _z([1, 2, 3, 4, 5])})
    other = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_growth_scores(
        _rev_growth(other), eps, _fcf_growth(other), _margin_exp(other), _eps_accel(other),
        revenue_growth_weight=0.0,
        eps_growth_weight=1.0,
        fcf_growth_weight=0.0,
        margin_expansion_weight=0.0,
        eps_acceleration_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("growth_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_fcf_falls_back():
    """FCF is undefined for cash-burning companies; composite should still work."""
    fcf_rows = [
        {"ticker": t, "date": DATES[0], "fcf_growth_3y_cagr_score": np.nan if t == "A" else 0.5}
        for t in TICKERS
    ]
    fcf = pd.DataFrame(fcf_rows)
    rev = _rev_growth({DATES[0]: _z([5, 4, 3, 2, 1])})
    eps = _eps_growth({DATES[0]: _z([5, 4, 3, 2, 1])})
    margin = _margin_exp({DATES[0]: _z([5, 4, 3, 2, 1])})
    accel = _eps_accel({DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_growth_scores(rev, eps, fcf, margin, accel)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["growth_score"])


def test_missing_eps_accel_falls_back():
    """EPS acceleration requires two periods of positive-base EPS; may be sparse."""
    accel_rows = [
        {"ticker": t, "date": DATES[0], "eps_growth_acceleration_score": np.nan if t == "C" else 0.2}
        for t in TICKERS
    ]
    accel = pd.DataFrame(accel_rows)
    r = compute_growth_scores(
        _rev_growth(), _eps_growth(), _fcf_growth(), _margin_exp(), accel,
    )
    c = r[(r["ticker"] == "C") & (r["date"] == DATES[0])]
    assert len(c) == 1
    assert not pd.isna(c.iloc[0]["growth_score"])


def test_all_missing_dropped():
    all_nan = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "revenue_growth_3y_cagr_score": np.nan},
        {"ticker": "B", "date": DATES[0], "revenue_growth_3y_cagr_score": 1.0},
    ])
    eps = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "eps_growth_3y_cagr_score": np.nan},
        {"ticker": "B", "date": DATES[0], "eps_growth_3y_cagr_score": 1.0},
    ])
    fcf = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "fcf_growth_3y_cagr_score": np.nan},
        {"ticker": "B", "date": DATES[0], "fcf_growth_3y_cagr_score": 1.0},
    ])
    margin = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "operating_margin_expansion_yoy_score": np.nan},
        {"ticker": "B", "date": DATES[0], "operating_margin_expansion_yoy_score": 1.0},
    ])
    accel = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "eps_growth_acceleration_score": np.nan},
        {"ticker": "B", "date": DATES[0], "eps_growth_acceleration_score": 1.0},
    ])
    r = compute_growth_scores(all_nan, eps, fcf, margin, accel)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_revenue_growth_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="revenue_growth_scores missing"):
        compute_growth_scores(bad, _eps_growth(), _fcf_growth(), _margin_exp(), _eps_accel())


def test_missing_eps_growth_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="eps_growth_scores missing"):
        compute_growth_scores(_rev_growth(), bad, _fcf_growth(), _margin_exp(), _eps_accel())


def test_missing_fcf_growth_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="fcf_growth_scores missing"):
        compute_growth_scores(_rev_growth(), _eps_growth(), bad, _margin_exp(), _eps_accel())


def test_missing_margin_expansion_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="margin_expansion_scores missing"):
        compute_growth_scores(_rev_growth(), _eps_growth(), _fcf_growth(), bad, _eps_accel())


def test_missing_eps_acceleration_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="eps_acceleration_scores missing"):
        compute_growth_scores(_rev_growth(), _eps_growth(), _fcf_growth(), _margin_exp(), bad)

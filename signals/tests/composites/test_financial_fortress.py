"""Tests for signals/composites/financial_fortress.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.financial_fortress import compute_financial_fortress_scores


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


def _nd(vbd=None):
    return _make("net_debt_to_ebitda_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})

def _ic(vbd=None):
    return _make("interest_coverage_score", vbd or {d: _z([2, 4, 1, 5, 3]) for d in DATES})

def _cr(vbd=None):
    return _make("current_ratio_score", vbd or {d: _z([3, 1, 5, 2, 4]) for d in DATES})

def _qr(vbd=None):
    return _make("quick_ratio_score", vbd or {d: _z([4, 3, 2, 5, 1]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_financial_fortress_scores(_nd(), _ic(), _cr(), _qr())
    assert set(r.columns) >= {
        "ticker", "date",
        "net_debt_to_ebitda_score", "interest_coverage_score",
        "current_ratio_score", "quick_ratio_score",
        "financial_fortress_score",
    }

def test_output_shape():
    r = compute_financial_fortress_scores(_nd(), _ic(), _cr(), _qr())
    assert len(r) == len(TICKERS) * len(DATES)

def test_output_sorted():
    r = compute_financial_fortress_scores(_nd(), _ic(), _cr(), _qr())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_financial_fortress_scores(_nd(), _ic(), _cr(), _qr())
    for d in DATES:
        s = r.loc[r["date"] == d, "financial_fortress_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Intuitive ordering ────────────────────────────────────────────────────────

def test_best_on_all_signals_top():
    nd = _make("net_debt_to_ebitda_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ic = _make("interest_coverage_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    cr = _make("current_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qr = _make("quick_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_financial_fortress_scores(nd, ic, cr, qr)
    top = r[r["date"] == DATES[0]].nlargest(1, "financial_fortress_score")
    assert top.iloc[0]["ticker"] == "A"

def test_worst_on_all_signals_bottom():
    nd = _make("net_debt_to_ebitda_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ic = _make("interest_coverage_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    cr = _make("current_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qr = _make("quick_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_financial_fortress_scores(nd, ic, cr, qr)
    bot = r[r["date"] == DATES[0]].nsmallest(1, "financial_fortress_score")
    assert bot.iloc[0]["ticker"] == "E"

def test_pure_net_debt_weight():
    nd = _make("net_debt_to_ebitda_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    ic = _make("interest_coverage_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    cr = _make("current_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qr = _make("quick_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_financial_fortress_scores(
        nd, ic, cr, qr,
        net_debt_ebitda_weight=1.0,
        interest_coverage_weight=0.0,
        current_ratio_weight=0.0,
        quick_ratio_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("financial_fortress_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_one_signal_falls_back():
    nd = _make("net_debt_to_ebitda_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ic = _make("interest_coverage_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    cr_rows = [
        {"ticker": t, "date": DATES[0], "current_ratio_score": np.nan if t == "A" else float(i)}
        for i, t in enumerate(TICKERS)
    ]
    cr = pd.DataFrame(cr_rows)
    qr = _make("quick_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_financial_fortress_scores(nd, ic, cr, qr)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["financial_fortress_score"])

def test_all_missing_dropped():
    nd = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "net_debt_to_ebitda_score": np.nan},
        {"ticker": "B", "date": DATES[0], "net_debt_to_ebitda_score": 1.0},
    ])
    ic = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "interest_coverage_score": np.nan},
        {"ticker": "B", "date": DATES[0], "interest_coverage_score": 0.5},
    ])
    cr = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "current_ratio_score": np.nan},
        {"ticker": "B", "date": DATES[0], "current_ratio_score": 0.5},
    ])
    qr = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quick_ratio_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quick_ratio_score": 0.5},
    ])
    r = compute_financial_fortress_scores(nd, ic, cr, qr)
    assert "A" not in r["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    nd = pd.DataFrame([{"ticker": "A", "date": DATES[0], "net_debt_to_ebitda_score": 1.0}])
    ic = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "interest_coverage_score": 0.5},
        {"ticker": "B", "date": DATES[0], "interest_coverage_score": -0.5},
    ])
    cr = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "current_ratio_score": 0.3},
        {"ticker": "B", "date": DATES[0], "current_ratio_score": -0.3},
    ])
    qr = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quick_ratio_score": 0.2},
        {"ticker": "B", "date": DATES[0], "quick_ratio_score": -0.2},
    ])
    r = compute_financial_fortress_scores(nd, ic, cr, qr)
    assert "B" in r["ticker"].values
    b = r[(r["ticker"] == "B") & (r["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["financial_fortress_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_net_debt_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="net_debt_ebitda_scores missing"):
        compute_financial_fortress_scores(bad, _ic(), _cr(), _qr())

def test_missing_interest_coverage_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="interest_coverage_scores missing"):
        compute_financial_fortress_scores(_nd(), bad, _cr(), _qr())

def test_missing_current_ratio_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="current_ratio_scores missing"):
        compute_financial_fortress_scores(_nd(), _ic(), bad, _qr())

def test_missing_quick_ratio_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="quick_ratio_scores missing"):
        compute_financial_fortress_scores(_nd(), _ic(), _cr(), bad)

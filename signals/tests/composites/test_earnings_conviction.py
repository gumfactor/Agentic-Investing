"""Tests for signals/composites/earnings_conviction.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.earnings_conviction import compute_earnings_conviction_scores


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


def _sloan(vbd=None):
    return _make("sloan_accrual_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})

def _cer(vbd=None):
    return _make("cash_earnings_ratio_score", vbd or {d: _z([2, 4, 1, 5, 3]) for d in DATES})

def _ec(vbd=None):
    return _make("earnings_consistency_score", vbd or {d: _z([3, 1, 5, 2, 4]) for d in DATES})

def _eps_stab(vbd=None):
    return _make("eps_stability_score", vbd or {d: _z([4, 3, 2, 5, 1]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_earnings_conviction_scores(_sloan(), _cer(), _ec(), _eps_stab())
    assert set(r.columns) >= {
        "ticker", "date",
        "sloan_accrual_score", "cash_earnings_ratio_score",
        "earnings_consistency_score", "eps_stability_score",
        "earnings_conviction_score",
    }

def test_output_shape():
    r = compute_earnings_conviction_scores(_sloan(), _cer(), _ec(), _eps_stab())
    assert len(r) == len(TICKERS) * len(DATES)

def test_output_sorted():
    r = compute_earnings_conviction_scores(_sloan(), _cer(), _ec(), _eps_stab())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_earnings_conviction_scores(_sloan(), _cer(), _ec(), _eps_stab())
    for d in DATES:
        s = r.loc[r["date"] == d, "earnings_conviction_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Intuitive ordering ────────────────────────────────────────────────────────

def test_best_on_all_top():
    sl = _make("sloan_accrual_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ce = _make("cash_earnings_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ec = _make("earnings_consistency_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    es = _make("eps_stability_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_earnings_conviction_scores(sl, ce, ec, es)
    top = r[r["date"] == DATES[0]].nlargest(1, "earnings_conviction_score")
    assert top.iloc[0]["ticker"] == "A"

def test_worst_on_all_bottom():
    sl = _make("sloan_accrual_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ce = _make("cash_earnings_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ec = _make("earnings_consistency_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    es = _make("eps_stability_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_earnings_conviction_scores(sl, ce, ec, es)
    bot = r[r["date"] == DATES[0]].nsmallest(1, "earnings_conviction_score")
    assert bot.iloc[0]["ticker"] == "E"

def test_accrual_signals_carry_most_weight():
    """With both accrual signals high for A and stability signals reversed,
    A should still rank first because sloan+cash_earnings = 60% combined weight."""
    sl = _make("sloan_accrual_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ce = _make("cash_earnings_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ec = _make("earnings_consistency_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    es = _make("eps_stability_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    r = compute_earnings_conviction_scores(sl, ce, ec, es)
    top = r[r["date"] == DATES[0]].nlargest(1, "earnings_conviction_score")
    assert top.iloc[0]["ticker"] == "A"

def test_pure_sloan_weight():
    sl = _make("sloan_accrual_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    ce = _make("cash_earnings_ratio_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    ec = _make("earnings_consistency_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    es = _make("eps_stability_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_earnings_conviction_scores(
        sl, ce, ec, es,
        sloan_accrual_weight=1.0,
        cash_earnings_ratio_weight=0.0,
        earnings_consistency_weight=0.0,
        eps_stability_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("earnings_conviction_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data ──────────────────────────────────────────────────────────────

def test_missing_one_signal_falls_back():
    sl = _make("sloan_accrual_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    cer_rows = [
        {"ticker": t, "date": DATES[0], "cash_earnings_ratio_score": np.nan if t == "A" else float(i)}
        for i, t in enumerate(TICKERS)
    ]
    ce = pd.DataFrame(cer_rows)
    ec = _make("earnings_consistency_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    es = _make("eps_stability_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_earnings_conviction_scores(sl, ce, ec, es)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["earnings_conviction_score"])

def test_all_missing_dropped():
    sl = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "sloan_accrual_score": np.nan},
        {"ticker": "B", "date": DATES[0], "sloan_accrual_score": 1.0},
    ])
    ce = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "cash_earnings_ratio_score": np.nan},
        {"ticker": "B", "date": DATES[0], "cash_earnings_ratio_score": 0.5},
    ])
    ec = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "earnings_consistency_score": np.nan},
        {"ticker": "B", "date": DATES[0], "earnings_consistency_score": 0.5},
    ])
    es = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "eps_stability_score": np.nan},
        {"ticker": "B", "date": DATES[0], "eps_stability_score": 0.5},
    ])
    r = compute_earnings_conviction_scores(sl, ce, ec, es)
    assert "A" not in r["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    sl = pd.DataFrame([{"ticker": "A", "date": DATES[0], "sloan_accrual_score": 1.0}])
    ce = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "cash_earnings_ratio_score": 0.5},
        {"ticker": "B", "date": DATES[0], "cash_earnings_ratio_score": -0.5},
    ])
    ec = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "earnings_consistency_score": 0.3},
        {"ticker": "B", "date": DATES[0], "earnings_consistency_score": -0.3},
    ])
    es = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "eps_stability_score": 0.2},
        {"ticker": "B", "date": DATES[0], "eps_stability_score": -0.2},
    ])
    r = compute_earnings_conviction_scores(sl, ce, ec, es)
    assert "B" in r["ticker"].values
    b = r[(r["ticker"] == "B") & (r["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["earnings_conviction_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_sloan_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="sloan_accrual_scores missing"):
        compute_earnings_conviction_scores(bad, _cer(), _ec(), _eps_stab())

def test_missing_cash_earnings_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="cash_earnings_ratio_scores missing"):
        compute_earnings_conviction_scores(_sloan(), bad, _ec(), _eps_stab())

def test_missing_earnings_consistency_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="earnings_consistency_scores missing"):
        compute_earnings_conviction_scores(_sloan(), _cer(), bad, _eps_stab())

def test_missing_eps_stability_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="eps_stability_scores missing"):
        compute_earnings_conviction_scores(_sloan(), _cer(), _ec(), bad)

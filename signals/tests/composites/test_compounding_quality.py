"""Tests for signals/composites/compounding_quality.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.compounding_quality import compute_compounding_quality_scores


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


def _roic(vbd=None):
    return _make("roic_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})

def _roce(vbd=None):
    return _make("roce_score", vbd or {d: _z([2, 4, 1, 5, 3]) for d in DATES})

def _gm(vbd=None):
    return _make("gross_margin_score", vbd or {d: _z([3, 1, 5, 2, 4]) for d in DATES})

def _om(vbd=None):
    return _make("operating_margin_score", vbd or {d: _z([4, 3, 2, 5, 1]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_compounding_quality_scores(_roic(), _roce(), _gm(), _om())
    assert set(r.columns) >= {
        "ticker", "date",
        "roic_score", "roce_score",
        "gross_margin_score", "operating_margin_score",
        "compounding_quality_score",
    }

def test_output_shape():
    r = compute_compounding_quality_scores(_roic(), _roce(), _gm(), _om())
    assert len(r) == len(TICKERS) * len(DATES)

def test_output_sorted():
    r = compute_compounding_quality_scores(_roic(), _roce(), _gm(), _om())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_compounding_quality_scores(_roic(), _roce(), _gm(), _om())
    for d in DATES:
        s = r.loc[r["date"] == d, "compounding_quality_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Intuitive ordering ────────────────────────────────────────────────────────

def test_best_on_all_top():
    ri = _make("roic_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    rc = _make("roce_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    gm = _make("gross_margin_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    om = _make("operating_margin_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_compounding_quality_scores(ri, rc, gm, om)
    top = r[r["date"] == DATES[0]].nlargest(1, "compounding_quality_score")
    assert top.iloc[0]["ticker"] == "A"

def test_pure_roic_weight():
    ri = _make("roic_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    rc = _make("roce_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    gm = _make("gross_margin_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    om = _make("operating_margin_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_compounding_quality_scores(
        ri, rc, gm, om,
        roic_weight=1.0, roce_weight=0.0,
        gross_margin_weight=0.0, operating_margin_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("compounding_quality_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]

def test_roic_dominates_conflicting_signals():
    """With default 40% ROIC weight, ROIC ranking should drive the top position
    when ROIC strongly agrees with ROCE but margins conflict."""
    ri = _make("roic_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    rc = _make("roce_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    gm = _make("gross_margin_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    om = _make("operating_margin_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    r = compute_compounding_quality_scores(ri, rc, gm, om)
    top = r[r["date"] == DATES[0]].nlargest(1, "compounding_quality_score")
    assert top.iloc[0]["ticker"] == "A"


# ── Missing data ──────────────────────────────────────────────────────────────

def test_missing_one_signal_falls_back():
    ri = _make("roic_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    rc_rows = [
        {"ticker": t, "date": DATES[0], "roce_score": np.nan if t == "A" else float(i)}
        for i, t in enumerate(TICKERS)
    ]
    rc = pd.DataFrame(rc_rows)
    gm = _make("gross_margin_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    om = _make("operating_margin_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r = compute_compounding_quality_scores(ri, rc, gm, om)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["compounding_quality_score"])

def test_all_missing_dropped():
    ri = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "roic_score": np.nan},
        {"ticker": "B", "date": DATES[0], "roic_score": 1.0},
    ])
    rc = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "roce_score": np.nan},
        {"ticker": "B", "date": DATES[0], "roce_score": 0.5},
    ])
    gm = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "gross_margin_score": np.nan},
        {"ticker": "B", "date": DATES[0], "gross_margin_score": 0.5},
    ])
    om = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "operating_margin_score": np.nan},
        {"ticker": "B", "date": DATES[0], "operating_margin_score": 0.5},
    ])
    r = compute_compounding_quality_scores(ri, rc, gm, om)
    assert "A" not in r["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    ri = pd.DataFrame([{"ticker": "A", "date": DATES[0], "roic_score": 1.0}])
    rc = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "roce_score": 0.5},
        {"ticker": "B", "date": DATES[0], "roce_score": -0.5},
    ])
    gm = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "gross_margin_score": 0.3},
        {"ticker": "B", "date": DATES[0], "gross_margin_score": -0.3},
    ])
    om = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "operating_margin_score": 0.2},
        {"ticker": "B", "date": DATES[0], "operating_margin_score": -0.2},
    ])
    r = compute_compounding_quality_scores(ri, rc, gm, om)
    assert "B" in r["ticker"].values
    b = r[(r["ticker"] == "B") & (r["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["compounding_quality_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_roic_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="roic_scores missing"):
        compute_compounding_quality_scores(bad, _roce(), _gm(), _om())

def test_missing_roce_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="roce_scores missing"):
        compute_compounding_quality_scores(_roic(), bad, _gm(), _om())

def test_missing_gross_margin_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="gross_margin_scores missing"):
        compute_compounding_quality_scores(_roic(), _roce(), bad, _om())

def test_missing_operating_margin_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="operating_margin_scores missing"):
        compute_compounding_quality_scores(_roic(), _roce(), _gm(), bad)

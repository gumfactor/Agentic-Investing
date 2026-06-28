"""Tests for signals/composites/sustainable_growth.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.sustainable_growth import compute_sustainable_growth_scores


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


def _growth(vbd=None):
    return _make("growth_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _quality(vbd=None):
    return _make("quality_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _roic_imp(vbd=None):
    return _make("roic_improvement_yoy_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_sustainable_growth_scores(_growth(), _quality(), _roic_imp())
    assert set(r.columns) >= {
        "ticker", "date",
        "growth_score", "quality_score", "roic_improvement_yoy_score",
        "sustainable_growth_score",
    }


def test_output_shape():
    r = compute_sustainable_growth_scores(_growth(), _quality(), _roic_imp())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_sustainable_growth_scores(_growth(), _quality(), _roic_imp())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_sustainable_growth_scores(_growth(), _quality(), _roic_imp())
    for d in DATES:
        s = r.loc[r["date"] == d, "sustainable_growth_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Directional correctness ───────────────────────────────────────────────────

def test_top_on_all_tops_composite():
    """Highest growth + quality + ROIC improvement = top composite."""
    best = {DATES[0]: _z([5, 4, 3, 2, 1])}   # A = best on all
    r = compute_sustainable_growth_scores(
        _growth(best), _quality(best), _roic_imp(best),
    )
    top = r[r["date"] == DATES[0]].nlargest(1, "sustainable_growth_score")
    assert top.iloc[0]["ticker"] == "A"


def test_high_quality_boosts_equally_growing():
    """Two tickers with equal growth: higher quality should win."""
    growth = _make("growth_score", {DATES[0]: _z([3, 3, 3, 3, 3])})      # all equal
    roic = _make("roic_improvement_yoy_score", {DATES[0]: _z([3, 3, 3, 3, 3])})  # all equal
    quality = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})    # A = highest
    r = compute_sustainable_growth_scores(growth, quality, roic)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["sustainable_growth_score"]
    assert scores["A"] > scores["E"]


def test_high_roic_improvement_boosts_equally_growing():
    """Two tickers with equal growth and quality: higher ROIC improvement should win."""
    growth = _make("growth_score", {DATES[0]: _z([3, 3, 3, 3, 3])})
    quality = _make("quality_score", {DATES[0]: _z([3, 3, 3, 3, 3])})
    roic = _make("roic_improvement_yoy_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # A = best
    r = compute_sustainable_growth_scores(growth, quality, roic)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["sustainable_growth_score"]
    assert scores["A"] > scores["E"]


def test_pure_growth_weight():
    """With growth_weight=1, composite rank == growth rank."""
    growth = _make("growth_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}   # would favour E
    r = compute_sustainable_growth_scores(
        growth,
        _quality(oppose), _roic_imp(oppose),
        growth_weight=1.0, quality_weight=0.0, roic_improvement_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("sustainable_growth_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_roic_falls_back():
    """ROIC improvement may be missing for young companies; composite must still work."""
    roic_rows = [
        {"ticker": t, "date": DATES[0], "roic_improvement_yoy_score": np.nan if t == "B" else 0.3}
        for t in TICKERS
    ]
    roic = pd.DataFrame(roic_rows)
    r = compute_sustainable_growth_scores(_growth(), _quality(), roic)
    b = r[(r["ticker"] == "B") & (r["date"] == DATES[0])]
    assert len(b) == 1
    assert not pd.isna(b.iloc[0]["sustainable_growth_score"])


def test_all_missing_dropped():
    g = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "growth_score": np.nan},
        {"ticker": "B", "date": DATES[0], "growth_score": 1.0},
    ])
    q = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quality_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quality_score": 1.0},
    ])
    roic = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "roic_improvement_yoy_score": np.nan},
        {"ticker": "B", "date": DATES[0], "roic_improvement_yoy_score": 0.5},
    ])
    r = compute_sustainable_growth_scores(g, q, roic)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_growth_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="growth_scores missing"):
        compute_sustainable_growth_scores(bad, _quality(), _roic_imp())


def test_missing_quality_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="quality_scores missing"):
        compute_sustainable_growth_scores(_growth(), bad, _roic_imp())


def test_missing_roic_improvement_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="roic_improvement_scores missing"):
        compute_sustainable_growth_scores(_growth(), _quality(), bad)

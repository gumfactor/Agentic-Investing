"""Tests for signals/composites/relative_strength.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.relative_strength import compute_relative_strength_scores


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


def _rs_12m(vbd=None):
    return _make("rel_strength_vs_spy_12m_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _rs_3m(vbd=None):
    return _make("rel_strength_vs_spy_3m_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _ma_slope(vbd=None):
    return _make("ma_slope_200_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_relative_strength_scores(_rs_12m(), _rs_3m(), _ma_slope())
    assert set(r.columns) >= {
        "ticker", "date",
        "rel_strength_vs_spy_12m_score", "rel_strength_vs_spy_3m_score",
        "ma_slope_200_score", "relative_strength_score",
    }


def test_output_shape():
    r = compute_relative_strength_scores(_rs_12m(), _rs_3m(), _ma_slope())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_relative_strength_scores(_rs_12m(), _rs_3m(), _ma_slope())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_relative_strength_scores(_rs_12m(), _rs_3m(), _ma_slope())
    for d in DATES:
        s = r.loc[r["date"] == d, "relative_strength_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Directional correctness ───────────────────────────────────────────────────

def test_top_rs_on_all_tops_composite():
    """Best relative strength on all three signals = top composite."""
    best = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_relative_strength_scores(_rs_12m(best), _rs_3m(best), _ma_slope(best))
    top = r[r["date"] == DATES[0]].nlargest(1, "relative_strength_score")
    assert top.iloc[0]["ticker"] == "A"


def test_strong_12m_rs_boosts_score():
    """Equal 3m RS and MA slope: stronger 12m RS → higher composite."""
    rs12 = _make("rel_strength_vs_spy_12m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    equal = {DATES[0]: [0.0, 0.0, 0.0, 0.0, 0.0]}
    r = compute_relative_strength_scores(rs12, _rs_3m(equal), _ma_slope(equal))
    scores = r[r["date"] == DATES[0]].set_index("ticker")["relative_strength_score"]
    assert scores["A"] > scores["E"]


def test_pure_12m_rs_weight():
    """With rs_12m_weight=1, rank == 12m RS rank."""
    rs12 = _make("rel_strength_vs_spy_12m_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_relative_strength_scores(
        rs12, _rs_3m(oppose), _ma_slope(oppose),
        rel_strength_12m_weight=1.0, rel_strength_3m_weight=0.0, ma_slope_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("relative_strength_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


def test_pure_3m_rs_weight():
    """With rs_3m_weight=1, rank == 3m RS rank."""
    rs3 = _make("rel_strength_vs_spy_3m_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_relative_strength_scores(
        _rs_12m(oppose), rs3, _ma_slope(oppose),
        rel_strength_12m_weight=0.0, rel_strength_3m_weight=1.0, ma_slope_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("relative_strength_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_ma_slope_falls_back():
    slope_rows = [
        {"ticker": t, "date": DATES[0], "ma_slope_200_score": np.nan if t == "C" else 0.3}
        for t in TICKERS
    ]
    slope = pd.DataFrame(slope_rows)
    r = compute_relative_strength_scores(_rs_12m(), _rs_3m(), slope)
    c = r[(r["ticker"] == "C") & (r["date"] == DATES[0])]
    assert len(c) == 1
    assert not pd.isna(c.iloc[0]["relative_strength_score"])


def test_all_missing_dropped():
    rs12 = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "rel_strength_vs_spy_12m_score": np.nan},
        {"ticker": "B", "date": DATES[0], "rel_strength_vs_spy_12m_score": 1.0},
    ])
    rs3 = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "rel_strength_vs_spy_3m_score": np.nan},
        {"ticker": "B", "date": DATES[0], "rel_strength_vs_spy_3m_score": 0.5},
    ])
    slope = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "ma_slope_200_score": np.nan},
        {"ticker": "B", "date": DATES[0], "ma_slope_200_score": 0.3},
    ])
    r = compute_relative_strength_scores(rs12, rs3, slope)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_rs_12m_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="rel_strength_12m_scores missing"):
        compute_relative_strength_scores(bad, _rs_3m(), _ma_slope())


def test_missing_rs_3m_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="rel_strength_3m_scores missing"):
        compute_relative_strength_scores(_rs_12m(), bad, _ma_slope())


def test_missing_ma_slope_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="ma_slope_scores missing"):
        compute_relative_strength_scores(_rs_12m(), _rs_3m(), bad)

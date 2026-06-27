"""Tests for signals/composites/trend_strength.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.trend_strength import compute_trend_strength_scores


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


def _ma_cross(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([1, 2, 3, 4, 5]) for d in DATES}
    return _make_scores("ma_cross_50_200_score", values_by_date)


def _trend_r2(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([2, 4, 1, 5, 3]) for d in DATES}
    return _make_scores("trend_r2_50d_score", values_by_date)


def _trend_consistency(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([3, 1, 5, 2, 4]) for d in DATES}
    return _make_scores("trend_consistency_63d_score", values_by_date)


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    result = compute_trend_strength_scores(_ma_cross(), _trend_r2(), _trend_consistency())
    assert set(result.columns) >= {
        "ticker", "date",
        "ma_cross_50_200_score", "trend_r2_50d_score",
        "trend_consistency_63d_score", "trend_strength_score",
    }


def test_output_shape():
    result = compute_trend_strength_scores(_ma_cross(), _trend_r2(), _trend_consistency())
    assert len(result) == len(TICKERS) * len(DATES)


def test_output_sorted():
    result = compute_trend_strength_scores(_ma_cross(), _trend_r2(), _trend_consistency())
    assert list(result["date"]) == sorted(result["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    result = compute_trend_strength_scores(_ma_cross(), _trend_r2(), _trend_consistency())
    for d in DATES:
        scores = result.loc[result["date"] == d, "trend_strength_score"].dropna()
        assert abs(scores.mean()) < 1e-10
        assert abs(scores.std(ddof=1) - 1.0) < 1e-10


# ── Intuitive ordering ────────────────────────────────────────────────────────

def test_all_signals_agree_top():
    """Ticker ranked highest on all three signals should score highest."""
    ma = _make_scores("ma_cross_50_200_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r2 = _make_scores("trend_r2_50d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    tc = _make_scores("trend_consistency_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_trend_strength_scores(ma, r2, tc)
    top = result.loc[result["date"] == DATES[0]].nlargest(1, "trend_strength_score")
    assert top.iloc[0]["ticker"] == "A"


def test_all_signals_agree_bottom():
    """Ticker ranked lowest on all three signals should score lowest."""
    ma = _make_scores("ma_cross_50_200_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r2 = _make_scores("trend_r2_50d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    tc = _make_scores("trend_consistency_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_trend_strength_scores(ma, r2, tc)
    bot = result.loc[result["date"] == DATES[0]].nsmallest(1, "trend_strength_score")
    assert bot.iloc[0]["ticker"] == "E"


# ── MA cross dominance (40% weight) ──────────────────────────────────────────

def test_ma_cross_heaviest_weight():
    """With pure ma_cross_weight=1, composite rank == ma_cross rank."""
    ma = _make_scores("ma_cross_50_200_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    r2 = _make_scores("trend_r2_50d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    tc = _make_scores("trend_consistency_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_trend_strength_scores(
        ma, r2, tc,
        ma_cross_weight=1.0, trend_r2_weight=0.0, trend_consistency_weight=0.0,
    )
    d = result[result["date"] == DATES[0]].sort_values("trend_strength_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_one_signal_falls_back():
    """When one signal is NaN, weight redistributes to the other two."""
    ma = _make_scores("ma_cross_50_200_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    r2_rows = [
        {"ticker": t, "date": DATES[0], "trend_r2_50d_score": (np.nan if t == "A" else float(i))}
        for i, t in enumerate(TICKERS)
    ]
    r2 = pd.DataFrame(r2_rows)
    tc = _make_scores("trend_consistency_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_trend_strength_scores(ma, r2, tc)
    a_row = result[(result["ticker"] == "A") & (result["date"] == DATES[0])]
    assert len(a_row) == 1
    assert not pd.isna(a_row.iloc[0]["trend_strength_score"])


def test_all_missing_dropped():
    """Rows where all three signals are NaN are dropped."""
    ma = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "ma_cross_50_200_score": np.nan},
        {"ticker": "B", "date": DATES[0], "ma_cross_50_200_score": 1.0},
    ])
    r2 = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "trend_r2_50d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "trend_r2_50d_score": 0.5},
    ])
    tc = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "trend_consistency_63d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "trend_consistency_63d_score": 0.5},
    ])
    result = compute_trend_strength_scores(ma, r2, tc)
    assert "A" not in result["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    """Ticker present in only one input is kept with weight redistributed."""
    ma = pd.DataFrame([{"ticker": "A", "date": DATES[0], "ma_cross_50_200_score": 1.0}])
    r2 = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "trend_r2_50d_score": 0.5},
        {"ticker": "B", "date": DATES[0], "trend_r2_50d_score": -0.5},
    ])
    tc = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "trend_consistency_63d_score": 0.3},
        {"ticker": "B", "date": DATES[0], "trend_consistency_63d_score": -0.3},
    ])
    result = compute_trend_strength_scores(ma, r2, tc)
    assert "B" in result["ticker"].values
    b = result[(result["ticker"] == "B") & (result["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["trend_strength_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_ma_cross_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="ma_cross_scores missing"):
        compute_trend_strength_scores(bad, _trend_r2(), _trend_consistency())


def test_missing_trend_r2_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="trend_r2_scores missing"):
        compute_trend_strength_scores(_ma_cross(), bad, _trend_consistency())


def test_missing_trend_consistency_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="trend_consistency_scores missing"):
        compute_trend_strength_scores(_ma_cross(), _trend_r2(), bad)

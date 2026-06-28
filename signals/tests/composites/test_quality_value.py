"""Tests for signals/composites/quality_value.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.quality_value import compute_quality_value_scores


# ── Fixtures ──────────────────────────────────────────────────────────────────

TICKERS = ["A", "B", "C", "D", "E"]
DATES = [date(2024, 1, 31), date(2024, 2, 29)]


def _make_scores(col: str, values_by_date: dict) -> pd.DataFrame:
    """Build a minimal score DataFrame with one z-scored column per date."""
    rows = []
    for d, vals in values_by_date.items():
        for ticker, v in zip(TICKERS, vals):
            rows.append({"ticker": ticker, "date": d, col: v})
    return pd.DataFrame(rows)


def _z(arr):
    a = np.array(arr, dtype=float)
    return list((a - a.mean()) / a.std(ddof=1))


def _value_scores(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([1, 2, 3, 4, 5]) for d in DATES}
    return _make_scores("value_score", values_by_date)


def _quality_scores(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        # Deliberately not the inverse of _value_scores to avoid a degenerate 50/50 tie
        values_by_date = {d: _z([3, 5, 1, 4, 2]) for d in DATES}
    return _make_scores("quality_score", values_by_date)


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    result = compute_quality_value_scores(_value_scores(), _quality_scores())
    assert set(result.columns) >= {"ticker", "date", "value_score", "quality_score", "quality_value_score"}


def test_output_shape():
    result = compute_quality_value_scores(_value_scores(), _quality_scores())
    assert len(result) == len(TICKERS) * len(DATES)


def test_output_sorted():
    result = compute_quality_value_scores(_value_scores(), _quality_scores())
    assert list(result["date"]) == sorted(result["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    result = compute_quality_value_scores(_value_scores(), _quality_scores())
    for d in DATES:
        scores = result.loc[result["date"] == d, "quality_value_score"].dropna()
        assert abs(scores.mean()) < 1e-10
        assert abs(scores.std(ddof=1) - 1.0) < 1e-10


# ── Intuitive ordering ────────────────────────────────────────────────────────

def test_high_both_scores_highest():
    """A ticker with the highest value AND quality should have the highest QV score."""
    # A = highest value AND highest quality
    vs = _make_scores("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qs = _make_scores("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_quality_value_scores(vs, qs)
    top = result.loc[result["date"] == DATES[0]].nlargest(1, "quality_value_score")
    assert top.iloc[0]["ticker"] == "A"


def test_high_value_low_quality_middling():
    """High value + low quality should score middling (not top, not bottom)."""
    vs = _make_scores("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qs = _make_scores("quality_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    result = compute_quality_value_scores(vs, qs)
    scores = result.loc[result["date"] == DATES[0]].set_index("ticker")["quality_value_score"]
    # When value and quality are perfectly inversely ranked, all QV scores
    # should be near zero (they cancel out)
    assert all(abs(scores) < 1e-10)


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_quality_falls_back_to_value():
    """When quality is missing for a ticker, full weight goes to value."""
    vs = _make_scores("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qs_rows = [
        {"ticker": "A", "date": DATES[0], "quality_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quality_score": 1.0},
        {"ticker": "C", "date": DATES[0], "quality_score": 0.0},
        {"ticker": "D", "date": DATES[0], "quality_score": -1.0},
        {"ticker": "E", "date": DATES[0], "quality_score": -2.0},
    ]
    qs = pd.DataFrame(qs_rows)
    result = compute_quality_value_scores(vs, qs)
    # A's QV score should still be computable (from value_score alone)
    a_row = result[(result["ticker"] == "A") & (result["date"] == DATES[0])]
    assert len(a_row) == 1
    assert not pd.isna(a_row.iloc[0]["quality_value_score"])


def test_both_missing_dropped():
    """Rows where both value and quality are NaN are dropped from output."""
    vs = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "value_score": np.nan},
        {"ticker": "B", "date": DATES[0], "value_score": 1.0},
    ])
    qs = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quality_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quality_score": 0.5},
    ])
    result = compute_quality_value_scores(vs, qs)
    assert "A" not in result["ticker"].values


# ── Outer join: tickers in only one input ────────────────────────────────────

def test_outer_join_tickers():
    """Tickers in only one input are kept; missing leg gets NaN and weight redistributed."""
    vs = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "value_score": 1.0},
    ])
    qs = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quality_score": 0.5},
        {"ticker": "B", "date": DATES[0], "quality_score": -0.5},
    ])
    result = compute_quality_value_scores(vs, qs)
    # B has no value score — should still appear with quality weight fully applied
    assert "B" in result["ticker"].values
    b = result[(result["ticker"] == "B") & (result["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["quality_value_score"])


# ── Custom weights ────────────────────────────────────────────────────────────

def test_pure_value_weight():
    """With value_weight=1, quality_value_score should rank identically to value_score."""
    vs = _make_scores("value_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    qs = _make_scores("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_quality_value_scores(vs, qs, value_weight=1.0, quality_weight=0.0)
    d = result[result["date"] == DATES[0]].sort_values("quality_value_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_value_score_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong_col": 1.0}])
    with pytest.raises(ValueError, match="value_scores missing"):
        compute_quality_value_scores(bad, _quality_scores())


def test_missing_quality_score_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong_col": 1.0}])
    with pytest.raises(ValueError, match="quality_scores missing"):
        compute_quality_value_scores(_value_scores(), bad)

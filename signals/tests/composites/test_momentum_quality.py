"""Tests for signals/composites/momentum_quality.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.momentum_quality import compute_momentum_quality_scores


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


def _momentum_scores(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([1, 2, 3, 4, 5]) for d in DATES}
    return _make_scores("momentum_score", values_by_date)


def _quality_scores(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([5, 4, 3, 2, 1]) for d in DATES}
    return _make_scores("quality_score", values_by_date)


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    result = compute_momentum_quality_scores(_momentum_scores(), _quality_scores())
    assert set(result.columns) >= {
        "ticker", "date", "momentum_score", "quality_score", "momentum_quality_score"
    }


def test_output_shape():
    result = compute_momentum_quality_scores(_momentum_scores(), _quality_scores())
    assert len(result) == len(TICKERS) * len(DATES)


def test_output_sorted():
    result = compute_momentum_quality_scores(_momentum_scores(), _quality_scores())
    assert list(result["date"]) == sorted(result["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    result = compute_momentum_quality_scores(_momentum_scores(), _quality_scores())
    for d in DATES:
        scores = result.loc[result["date"] == d, "momentum_quality_score"].dropna()
        assert abs(scores.mean()) < 1e-10
        assert abs(scores.std(ddof=1) - 1.0) < 1e-10


# ── Momentum dominance (70 / 30 default) ─────────────────────────────────────

def test_momentum_dominates_ranking():
    """With default 70/30, momentum ranking should dominate the composite."""
    # Momentum: A=highest ... E=lowest. Quality: A=lowest ... E=highest.
    # Net effect: momentum advantage for A outweighs quality disadvantage.
    mom = _make_scores("momentum_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qua = _make_scores("quality_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    result = compute_momentum_quality_scores(mom, qua)
    top = result.loc[result["date"] == DATES[0]].nlargest(1, "momentum_quality_score")
    assert top.iloc[0]["ticker"] == "A"


def test_high_both_highest():
    """Highest momentum AND highest quality should rank #1 unambiguously."""
    mom = _make_scores("momentum_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qua = _make_scores("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_momentum_quality_scores(mom, qua)
    top = result.loc[result["date"] == DATES[0]].nlargest(1, "momentum_quality_score")
    assert top.iloc[0]["ticker"] == "A"


def test_pure_momentum_weight():
    """With momentum_weight=1, composite rank == momentum rank."""
    mom = _make_scores("momentum_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    qua = _make_scores("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_momentum_quality_scores(
        mom, qua, momentum_weight=1.0, quality_weight=0.0
    )
    d = result[result["date"] == DATES[0]].sort_values("momentum_quality_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_quality_falls_back_to_momentum():
    """When quality is NaN for a ticker, full weight goes to momentum_score."""
    mom = _make_scores("momentum_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    qua_rows = [
        {"ticker": "A", "date": DATES[0], "quality_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quality_score": 1.0},
        {"ticker": "C", "date": DATES[0], "quality_score": 0.0},
        {"ticker": "D", "date": DATES[0], "quality_score": -1.0},
        {"ticker": "E", "date": DATES[0], "quality_score": -2.0},
    ]
    qua = pd.DataFrame(qua_rows)
    result = compute_momentum_quality_scores(mom, qua)
    a_row = result[(result["ticker"] == "A") & (result["date"] == DATES[0])]
    assert len(a_row) == 1
    assert not pd.isna(a_row.iloc[0]["momentum_quality_score"])


def test_both_missing_dropped():
    mom = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "momentum_score": np.nan},
        {"ticker": "B", "date": DATES[0], "momentum_score": 1.0},
    ])
    qua = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quality_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quality_score": 0.5},
    ])
    result = compute_momentum_quality_scores(mom, qua)
    assert "A" not in result["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    """Tickers in only one input are kept with the available signal at full weight."""
    mom = pd.DataFrame([{"ticker": "A", "date": DATES[0], "momentum_score": 1.0}])
    qua = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quality_score": 0.5},
        {"ticker": "B", "date": DATES[0], "quality_score": -0.5},
    ])
    result = compute_momentum_quality_scores(mom, qua)
    assert "B" in result["ticker"].values
    b = result[(result["ticker"] == "B") & (result["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["momentum_quality_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_momentum_score_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong_col": 1.0}])
    with pytest.raises(ValueError, match="momentum_scores missing"):
        compute_momentum_quality_scores(bad, _quality_scores())


def test_missing_quality_score_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong_col": 1.0}])
    with pytest.raises(ValueError, match="quality_scores missing"):
        compute_momentum_quality_scores(_momentum_scores(), bad)

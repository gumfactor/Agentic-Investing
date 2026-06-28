"""Tests for signals/composites/volume_momentum.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.volume_momentum import compute_volume_momentum_scores


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


def _momentum(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([1, 2, 3, 4, 5]) for d in DATES}
    return _make_scores("momentum_score", values_by_date)


def _vwm(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([2, 4, 1, 5, 3]) for d in DATES}
    return _make_scores("volume_weighted_momentum_21d_score", values_by_date)


def _obv(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([3, 1, 5, 2, 4]) for d in DATES}
    return _make_scores("obv_momentum_21d_score", values_by_date)


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    result = compute_volume_momentum_scores(_momentum(), _vwm(), _obv())
    assert set(result.columns) >= {
        "ticker", "date",
        "momentum_score", "volume_weighted_momentum_21d_score",
        "obv_momentum_21d_score", "volume_momentum_score",
    }


def test_output_shape():
    result = compute_volume_momentum_scores(_momentum(), _vwm(), _obv())
    assert len(result) == len(TICKERS) * len(DATES)


def test_output_sorted():
    result = compute_volume_momentum_scores(_momentum(), _vwm(), _obv())
    assert list(result["date"]) == sorted(result["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    result = compute_volume_momentum_scores(_momentum(), _vwm(), _obv())
    for d in DATES:
        scores = result.loc[result["date"] == d, "volume_momentum_score"].dropna()
        assert abs(scores.mean()) < 1e-10
        assert abs(scores.std(ddof=1) - 1.0) < 1e-10


# ── Intuitive ordering ────────────────────────────────────────────────────────

def test_all_signals_agree_top():
    """Ticker highest on all three signals should score highest."""
    mom = _make_scores("momentum_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    vwm = _make_scores("volume_weighted_momentum_21d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    obv = _make_scores("obv_momentum_21d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_volume_momentum_scores(mom, vwm, obv)
    top = result.loc[result["date"] == DATES[0]].nlargest(1, "volume_momentum_score")
    assert top.iloc[0]["ticker"] == "A"


def test_momentum_dominates_with_pure_weight():
    """With momentum_weight=1, composite rank == momentum rank."""
    mom = _make_scores("momentum_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    vwm = _make_scores("volume_weighted_momentum_21d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    obv = _make_scores("obv_momentum_21d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_volume_momentum_scores(
        mom, vwm, obv,
        momentum_weight=1.0,
        volume_weighted_momentum_weight=0.0,
        obv_momentum_weight=0.0,
    )
    d = result[result["date"] == DATES[0]].sort_values("volume_momentum_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


def test_volume_confirmation_boosts_momentum():
    """A ticker with high momentum AND high volume signals should outscore
    a ticker with high momentum but low volume confirmation."""
    # A: high on all three; E: high momentum but low volume signals
    mom = _make_scores("momentum_score", {DATES[0]: _z([5, 3, 3, 3, 5])})
    vwm = _make_scores("volume_weighted_momentum_21d_score", {DATES[0]: _z([5, 3, 3, 3, 1])})
    obv = _make_scores("obv_momentum_21d_score", {DATES[0]: _z([5, 3, 3, 3, 1])})
    result = compute_volume_momentum_scores(mom, vwm, obv)
    scores = result[result["date"] == DATES[0]].set_index("ticker")["volume_momentum_score"]
    assert scores["A"] > scores["E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_obv_falls_back():
    """When OBV is NaN, weight redistributes to momentum and VWM."""
    mom = _make_scores("momentum_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    vwm = _make_scores("volume_weighted_momentum_21d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    obv_rows = [
        {"ticker": t, "date": DATES[0], "obv_momentum_21d_score": np.nan if t == "A" else float(i)}
        for i, t in enumerate(TICKERS)
    ]
    obv = pd.DataFrame(obv_rows)
    result = compute_volume_momentum_scores(mom, vwm, obv)
    a_row = result[(result["ticker"] == "A") & (result["date"] == DATES[0])]
    assert len(a_row) == 1
    assert not pd.isna(a_row.iloc[0]["volume_momentum_score"])


def test_all_missing_dropped():
    mom = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "momentum_score": np.nan},
        {"ticker": "B", "date": DATES[0], "momentum_score": 1.0},
    ])
    vwm = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "volume_weighted_momentum_21d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "volume_weighted_momentum_21d_score": 0.5},
    ])
    obv = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "obv_momentum_21d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "obv_momentum_21d_score": 0.5},
    ])
    result = compute_volume_momentum_scores(mom, vwm, obv)
    assert "A" not in result["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    mom = pd.DataFrame([{"ticker": "A", "date": DATES[0], "momentum_score": 1.0}])
    vwm = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "volume_weighted_momentum_21d_score": 0.5},
        {"ticker": "B", "date": DATES[0], "volume_weighted_momentum_21d_score": -0.5},
    ])
    obv = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "obv_momentum_21d_score": 0.3},
        {"ticker": "B", "date": DATES[0], "obv_momentum_21d_score": -0.3},
    ])
    result = compute_volume_momentum_scores(mom, vwm, obv)
    assert "B" in result["ticker"].values
    b = result[(result["ticker"] == "B") & (result["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["volume_momentum_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_momentum_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="momentum_scores missing"):
        compute_volume_momentum_scores(bad, _vwm(), _obv())


def test_missing_vwm_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="volume_weighted_momentum_scores missing"):
        compute_volume_momentum_scores(_momentum(), bad, _obv())


def test_missing_obv_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="obv_momentum_scores missing"):
        compute_volume_momentum_scores(_momentum(), _vwm(), bad)

"""Tests for signals/composites/growth_momentum.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.growth_momentum import compute_growth_momentum_scores


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


def _vol_adj_mom(vbd=None):
    return _make("vol_adjusted_mom_12m_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _eps_accel(vbd=None):
    return _make("eps_growth_acceleration_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_growth_momentum_scores(_growth(), _vol_adj_mom(), _eps_accel())
    assert set(r.columns) >= {
        "ticker", "date",
        "growth_score", "vol_adjusted_mom_12m_score", "eps_growth_acceleration_score",
        "growth_momentum_score",
    }


def test_output_shape():
    r = compute_growth_momentum_scores(_growth(), _vol_adj_mom(), _eps_accel())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_growth_momentum_scores(_growth(), _vol_adj_mom(), _eps_accel())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_growth_momentum_scores(_growth(), _vol_adj_mom(), _eps_accel())
    for d in DATES:
        s = r.loc[r["date"] == d, "growth_momentum_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Directional correctness ───────────────────────────────────────────────────

def test_top_on_all_tops_composite():
    """Highest growth + momentum + EPS acceleration = top composite."""
    best = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_growth_momentum_scores(_growth(best), _vol_adj_mom(best), _eps_accel(best))
    top = r[r["date"] == DATES[0]].nlargest(1, "growth_momentum_score")
    assert top.iloc[0]["ticker"] == "A"


def test_momentum_confirms_growth():
    """Equal growth but strong momentum + EPS acceleration should lift rank."""
    growth = _make("growth_score", {DATES[0]: [0.0, 0.0, 0.0, 0.0, 0.0]})   # all equal (at cross-sectional mean)
    mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})    # A = best
    accel = _make("eps_growth_acceleration_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # A = best
    r = compute_growth_momentum_scores(growth, mom, accel)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["growth_momentum_score"]
    assert scores["A"] > scores["E"]


def test_pure_growth_weight():
    """With growth_weight=1, composite rank == growth rank."""
    growth = _make("growth_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_growth_momentum_scores(
        growth,
        _vol_adj_mom(oppose), _eps_accel(oppose),
        growth_weight=1.0, vol_adj_mom_weight=0.0, eps_acceleration_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("growth_momentum_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


def test_pure_momentum_weight():
    """With vol_adj_mom_weight=1, composite rank == momentum rank."""
    mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_growth_momentum_scores(
        _growth(oppose), mom, _eps_accel(oppose),
        growth_weight=0.0, vol_adj_mom_weight=1.0, eps_acceleration_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("growth_momentum_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_eps_accel_falls_back():
    """EPS acceleration is often sparse; composite must still compute."""
    accel_rows = [
        {"ticker": t, "date": DATES[0], "eps_growth_acceleration_score": np.nan if t == "D" else 0.4}
        for t in TICKERS
    ]
    accel = pd.DataFrame(accel_rows)
    r = compute_growth_momentum_scores(_growth(), _vol_adj_mom(), accel)
    d_row = r[(r["ticker"] == "D") & (r["date"] == DATES[0])]
    assert len(d_row) == 1
    assert not pd.isna(d_row.iloc[0]["growth_momentum_score"])


def test_all_missing_dropped():
    g = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "growth_score": np.nan},
        {"ticker": "B", "date": DATES[0], "growth_score": 1.0},
    ])
    m = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "vol_adjusted_mom_12m_score": np.nan},
        {"ticker": "B", "date": DATES[0], "vol_adjusted_mom_12m_score": 0.5},
    ])
    a = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "eps_growth_acceleration_score": np.nan},
        {"ticker": "B", "date": DATES[0], "eps_growth_acceleration_score": 0.3},
    ])
    r = compute_growth_momentum_scores(g, m, a)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_growth_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="growth_scores missing"):
        compute_growth_momentum_scores(bad, _vol_adj_mom(), _eps_accel())


def test_missing_vol_adj_mom_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="vol_adj_mom_scores missing"):
        compute_growth_momentum_scores(_growth(), bad, _eps_accel())


def test_missing_eps_accel_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="eps_acceleration_scores missing"):
        compute_growth_momentum_scores(_growth(), _vol_adj_mom(), bad)

"""Tests for signals/composites/defensive_quality.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.defensive_quality import compute_defensive_quality_scores


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


def _quality(vbd=None):
    return _make("quality_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _beta(vbd=None):
    # Lower z-score = lower beta = more defensive = favoured
    return _make("beta_252d_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})


def _up_down_vol(vbd=None):
    return _make("up_down_vol_ratio_63d_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_defensive_quality_scores(_quality(), _beta(), _up_down_vol())
    assert set(r.columns) >= {
        "ticker", "date",
        "quality_score", "beta_252d_score", "up_down_vol_ratio_63d_score",
        "defensive_quality_score",
    }
    assert "_low_beta" not in r.columns


def test_output_shape():
    r = compute_defensive_quality_scores(_quality(), _beta(), _up_down_vol())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_defensive_quality_scores(_quality(), _beta(), _up_down_vol())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_defensive_quality_scores(_quality(), _beta(), _up_down_vol())
    for d in DATES:
        s = r.loc[r["date"] == d, "defensive_quality_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Contrarian direction: low beta boosts score ────────────────────────────────

def test_low_beta_boosts_score():
    """A ticker with lower beta should score higher than one with higher
    beta, given equal quality and up/down vol ratio."""
    qual = _make("quality_score", {DATES[0]: [0.0, 0.0, 0.0, 0.0, 0.0]})          # all equal (at cross-sectional mean)
    up_down = _make("up_down_vol_ratio_63d_score", {DATES[0]: [0.0, 0.0, 0.0, 0.0, 0.0]})  # all equal (at cross-sectional mean)
    beta = _make("beta_252d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})         # A = lowest beta
    r = compute_defensive_quality_scores(qual, beta, up_down)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["defensive_quality_score"]
    assert scores["A"] > scores["E"]


def test_high_quality_low_beta_top():
    """Highest quality + lowest beta + best up/down vol = top composite."""
    qual = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})            # A = best
    beta = _make("beta_252d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})           # A = lowest
    up_down = _make("up_down_vol_ratio_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # A = best
    r = compute_defensive_quality_scores(qual, beta, up_down)
    top = r[r["date"] == DATES[0]].nlargest(1, "defensive_quality_score")
    assert top.iloc[0]["ticker"] == "A"


def test_low_quality_high_beta_bottom():
    """Lowest quality + highest beta + worst up/down vol = bottom composite."""
    qual = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})            # E = worst
    beta = _make("beta_252d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})           # E = highest
    up_down = _make("up_down_vol_ratio_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # E = worst
    r = compute_defensive_quality_scores(qual, beta, up_down)
    bot = r[r["date"] == DATES[0]].nsmallest(1, "defensive_quality_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_pure_quality_weight():
    """With quality_weight=1, composite rank == quality rank."""
    qual = _make("quality_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    beta = _make("beta_252d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})        # would favour E
    up_down = _make("up_down_vol_ratio_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # would favour E
    r = compute_defensive_quality_scores(
        qual, beta, up_down,
        quality_weight=1.0, beta_weight=0.0, up_down_vol_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("defensive_quality_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Raw values preserved in output ────────────────────────────────────────────

def test_raw_beta_values_preserved():
    """Original beta_252d_score must be in the output, not negated."""
    beta_vals = _z([1.0, 2.0, 3.0, 4.0, 5.0])
    beta = _make("beta_252d_score", {DATES[0]: beta_vals})
    qual = _make("quality_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    up_down = _make("up_down_vol_ratio_63d_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    r = compute_defensive_quality_scores(qual, beta, up_down)
    d = r[r["date"] == DATES[0]].set_index("ticker")
    for ticker, expected in zip(TICKERS, beta_vals):
        assert abs(d.loc[ticker, "beta_252d_score"] - expected) < 1e-9


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_beta_falls_back():
    qual = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    beta_rows = [
        {"ticker": t, "date": DATES[0], "beta_252d_score": np.nan if t == "A" else 0.5}
        for t in TICKERS
    ]
    beta = pd.DataFrame(beta_rows)
    up_down = _make("up_down_vol_ratio_63d_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    r = compute_defensive_quality_scores(qual, beta, up_down)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["defensive_quality_score"])


def test_all_missing_dropped():
    qual = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quality_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quality_score": 1.0},
    ])
    beta = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "beta_252d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "beta_252d_score": 0.5},
    ])
    up_down = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "up_down_vol_ratio_63d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "up_down_vol_ratio_63d_score": 0.5},
    ])
    r = compute_defensive_quality_scores(qual, beta, up_down)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_quality_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="quality_scores missing"):
        compute_defensive_quality_scores(bad, _beta(), _up_down_vol())


def test_missing_beta_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="beta_scores missing"):
        compute_defensive_quality_scores(_quality(), bad, _up_down_vol())


def test_missing_up_down_vol_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="up_down_vol_scores missing"):
        compute_defensive_quality_scores(_quality(), _beta(), bad)

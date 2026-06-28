"""Tests for signals/composites/breakout_momentum.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.breakout_momentum import compute_breakout_momentum_scores


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


def _52w_high(vbd=None):
    return _make("price_vs_52w_high_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _donchian(vbd=None):
    return _make("donchian_pct_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _ma_cross(vbd=None):
    return _make("ma_cross_50_200_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_breakout_momentum_scores(_52w_high(), _donchian(), _ma_cross())
    assert set(r.columns) >= {
        "ticker", "date",
        "price_vs_52w_high_score", "donchian_pct_score", "ma_cross_50_200_score",
        "breakout_momentum_score",
    }


def test_output_shape():
    r = compute_breakout_momentum_scores(_52w_high(), _donchian(), _ma_cross())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_breakout_momentum_scores(_52w_high(), _donchian(), _ma_cross())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_breakout_momentum_scores(_52w_high(), _donchian(), _ma_cross())
    for d in DATES:
        s = r.loc[r["date"] == d, "breakout_momentum_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Directional correctness ───────────────────────────────────────────────────

def test_best_breakout_tops():
    """Highest 52w high proximity + Donchian position + MA cross = top composite."""
    best = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_breakout_momentum_scores(_52w_high(best), _donchian(best), _ma_cross(best))
    top = r[r["date"] == DATES[0]].nlargest(1, "breakout_momentum_score")
    assert top.iloc[0]["ticker"] == "A"


def test_worst_breakout_bottom():
    """Lowest scores across all three signals = bottom composite."""
    worst = {DATES[0]: _z([5, 4, 3, 2, 1])}  # E = worst
    r = compute_breakout_momentum_scores(_52w_high(worst), _donchian(worst), _ma_cross(worst))
    bot = r[r["date"] == DATES[0]].nsmallest(1, "breakout_momentum_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_near_52w_high_boosts_score():
    """Equal Donchian and MA cross: higher 52w high proximity → higher score."""
    high = _make("price_vs_52w_high_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    equal = {DATES[0]: _z([3, 3, 3, 3, 3])}
    r = compute_breakout_momentum_scores(
        high,
        _donchian(equal), _ma_cross(equal),
    )
    scores = r[r["date"] == DATES[0]].set_index("ticker")["breakout_momentum_score"]
    assert scores["A"] > scores["E"]


def test_pure_52w_high_weight():
    """With price_vs_52w_high_weight=1, rank == 52w high rank."""
    high = _make("price_vs_52w_high_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_breakout_momentum_scores(
        high, _donchian(oppose), _ma_cross(oppose),
        price_vs_52w_high_weight=1.0, donchian_weight=0.0, ma_cross_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("breakout_momentum_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_ma_cross_falls_back():
    ma_rows = [
        {"ticker": t, "date": DATES[0], "ma_cross_50_200_score": np.nan if t == "A" else 0.5}
        for t in TICKERS
    ]
    ma = pd.DataFrame(ma_rows)
    r = compute_breakout_momentum_scores(_52w_high(), _donchian(), ma)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["breakout_momentum_score"])


def test_all_missing_dropped():
    h = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "price_vs_52w_high_score": np.nan},
        {"ticker": "B", "date": DATES[0], "price_vs_52w_high_score": 1.0},
    ])
    d_ = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "donchian_pct_score": np.nan},
        {"ticker": "B", "date": DATES[0], "donchian_pct_score": 0.5},
    ])
    m = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "ma_cross_50_200_score": np.nan},
        {"ticker": "B", "date": DATES[0], "ma_cross_50_200_score": 0.5},
    ])
    r = compute_breakout_momentum_scores(h, d_, m)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_52w_high_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="price_vs_52w_high_scores missing"):
        compute_breakout_momentum_scores(bad, _donchian(), _ma_cross())


def test_missing_donchian_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="donchian_scores missing"):
        compute_breakout_momentum_scores(_52w_high(), bad, _ma_cross())


def test_missing_ma_cross_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="ma_cross_scores missing"):
        compute_breakout_momentum_scores(_52w_high(), _donchian(), bad)

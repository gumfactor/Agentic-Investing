"""Tests for signals/composites/deep_value_oversold.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.deep_value_oversold import compute_deep_value_oversold_scores


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


def _value(vbd=None):
    return _make("value_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})


def _rsi_raw(vbd=None):
    return _make("rsi_14_raw", vbd or {d: [60, 55, 50, 40, 30] for d in DATES})


def _bb_raw(vbd=None):
    # Raw %B values: 0 = at lower band, 1 = at upper band
    return _make("bb_pct_b_20_raw", vbd or {d: [0.8, 0.6, 0.5, 0.3, 0.1] for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_deep_value_oversold_scores(_value(), _rsi_raw(), _bb_raw())
    assert set(r.columns) >= {
        "ticker", "date",
        "value_score", "rsi_14_raw", "bb_pct_b_20_raw",
        "deep_value_oversold_score",
    }
    assert "_rsi_oversold" not in r.columns
    assert "_bb_oversold" not in r.columns


def test_output_shape():
    r = compute_deep_value_oversold_scores(_value(), _rsi_raw(), _bb_raw())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_deep_value_oversold_scores(_value(), _rsi_raw(), _bb_raw())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_deep_value_oversold_scores(_value(), _rsi_raw(), _bb_raw())
    for d in DATES:
        s = r.loc[r["date"] == d, "deep_value_oversold_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Contrarian direction: low %B and low RSI boost score ─────────────────────

def test_near_lower_band_boosts_score():
    """A ticker with low %B (near lower Bollinger band) should score higher than
    one near the upper band, given equal value."""
    val = _make("value_score", {DATES[0]: [0.0, 0.0, 0.0, 0.0, 0.0]})  # all equal (at cross-sectional mean)
    rsi = _make("rsi_14_raw", {DATES[0]: [50, 50, 50, 50, 50]})  # all equal
    bb = _make("bb_pct_b_20_raw", {DATES[0]: [0.05, 0.2, 0.5, 0.7, 0.95]})  # A near lower, E near upper
    r = compute_deep_value_oversold_scores(val, rsi, bb)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["deep_value_oversold_score"]
    assert scores["A"] > scores["E"]


def test_cheap_and_oversold_top():
    """Cheapest value + lowest RSI + lowest %B = highest score."""
    val = _make("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # A = cheapest
    rsi = _make("rsi_14_raw", {DATES[0]: [20, 30, 50, 60, 75]})    # A = most oversold
    bb = _make("bb_pct_b_20_raw", {DATES[0]: [0.05, 0.2, 0.5, 0.7, 0.9]})  # A near lower band
    r = compute_deep_value_oversold_scores(val, rsi, bb)
    top = r[r["date"] == DATES[0]].nlargest(1, "deep_value_oversold_score")
    assert top.iloc[0]["ticker"] == "A"


def test_expensive_and_overbought_bottom():
    """Most expensive + highest RSI + highest %B = lowest score."""
    val = _make("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # A = cheapest, E = most expensive
    rsi = _make("rsi_14_raw", {DATES[0]: [20, 30, 50, 65, 80]})    # E = most overbought
    bb = _make("bb_pct_b_20_raw", {DATES[0]: [0.05, 0.2, 0.5, 0.75, 0.95]})  # E near upper
    r = compute_deep_value_oversold_scores(val, rsi, bb)
    bot = r[r["date"] == DATES[0]].nsmallest(1, "deep_value_oversold_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_pure_value_weight():
    """With value_weight=1, composite rank == value rank."""
    val = _make("value_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    rsi = _make("rsi_14_raw", {DATES[0]: [80, 70, 50, 30, 20]})   # would favour E
    bb = _make("bb_pct_b_20_raw", {DATES[0]: [0.9, 0.7, 0.5, 0.2, 0.05]})  # would favour E
    r = compute_deep_value_oversold_scores(
        val, rsi, bb,
        value_weight=1.0, rsi_weight=0.0, bb_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("deep_value_oversold_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Raw values preserved in output ────────────────────────────────────────────

def test_raw_bb_values_preserved():
    """Original bb_pct_b_20_raw values must be in the output, not negated."""
    bb_vals = [0.05, 0.2, 0.5, 0.75, 0.95]
    bb = _make("bb_pct_b_20_raw", {DATES[0]: bb_vals})
    val = _make("value_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    rsi = _make("rsi_14_raw", {DATES[0]: [50.0] * 5})
    r = compute_deep_value_oversold_scores(val, rsi, bb)
    d = r[r["date"] == DATES[0]].set_index("ticker")
    for ticker, expected in zip(TICKERS, bb_vals):
        assert abs(d.loc[ticker, "bb_pct_b_20_raw"] - expected) < 1e-9


def test_raw_rsi_values_preserved():
    """Original rsi_14_raw values must be in the output, not negated."""
    rsi_vals = [25.0, 35.0, 50.0, 65.0, 78.0]
    rsi = _make("rsi_14_raw", {DATES[0]: rsi_vals})
    val = _make("value_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    bb = _make("bb_pct_b_20_raw", {DATES[0]: [0.5] * 5})
    r = compute_deep_value_oversold_scores(val, rsi, bb)
    d = r[r["date"] == DATES[0]].set_index("ticker")
    for ticker, expected in zip(TICKERS, rsi_vals):
        assert abs(d.loc[ticker, "rsi_14_raw"] - expected) < 1e-9


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_bb_falls_back():
    val = _make("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    rsi = _make("rsi_14_raw", {DATES[0]: [30, 40, 50, 60, 70]})
    bb_rows = [
        {"ticker": t, "date": DATES[0], "bb_pct_b_20_raw": np.nan if t == "A" else 0.5}
        for t in TICKERS
    ]
    bb = pd.DataFrame(bb_rows)
    r = compute_deep_value_oversold_scores(val, rsi, bb)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["deep_value_oversold_score"])


def test_all_missing_dropped():
    val = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "value_score": np.nan},
        {"ticker": "B", "date": DATES[0], "value_score": 1.0},
    ])
    rsi = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "rsi_14_raw": np.nan},
        {"ticker": "B", "date": DATES[0], "rsi_14_raw": 50.0},
    ])
    bb = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "bb_pct_b_20_raw": np.nan},
        {"ticker": "B", "date": DATES[0], "bb_pct_b_20_raw": 0.5},
    ])
    r = compute_deep_value_oversold_scores(val, rsi, bb)
    assert "A" not in r["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    val = pd.DataFrame([{"ticker": "A", "date": DATES[0], "value_score": 1.0}])
    rsi = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "rsi_14_raw": 40.0},
        {"ticker": "B", "date": DATES[0], "rsi_14_raw": 25.0},
    ])
    bb = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "bb_pct_b_20_raw": 0.4},
        {"ticker": "B", "date": DATES[0], "bb_pct_b_20_raw": 0.1},
    ])
    r = compute_deep_value_oversold_scores(val, rsi, bb)
    assert "B" in r["ticker"].values
    b = r[(r["ticker"] == "B") & (r["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["deep_value_oversold_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_value_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="value_scores missing"):
        compute_deep_value_oversold_scores(bad, _rsi_raw(), _bb_raw())


def test_missing_rsi_raw_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="rsi_raw_scores missing"):
        compute_deep_value_oversold_scores(_value(), bad, _bb_raw())


def test_missing_bb_raw_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="bb_pct_b_raw_scores missing"):
        compute_deep_value_oversold_scores(_value(), _rsi_raw(), bad)

"""Tests for signals/composites/quality_dip.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.quality_dip import compute_quality_dip_scores


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
    # Pre-z-scored quality scores
    return _make("quality_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})


def _rsi_raw(vbd=None):
    # Raw RSI values (0–100); lower = more oversold
    return _make("rsi_14_raw", vbd or {d: [60, 55, 50, 40, 30] for d in DATES})


def _zscore_raw(vbd=None):
    # Raw time-series z-scores; lower = more depressed vs own history
    return _make("rolling_zscore_252d_raw", vbd or {d: [1.0, 0.5, 0.0, -0.5, -1.5] for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_quality_dip_scores(_quality(), _rsi_raw(), _zscore_raw())
    assert set(r.columns) >= {
        "ticker", "date",
        "quality_score", "rsi_14_raw", "rolling_zscore_252d_raw",
        "quality_dip_score",
    }
    # Internal negated columns must NOT appear in output
    assert "_rsi_oversold" not in r.columns
    assert "_price_depressed" not in r.columns


def test_output_shape():
    r = compute_quality_dip_scores(_quality(), _rsi_raw(), _zscore_raw())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_quality_dip_scores(_quality(), _rsi_raw(), _zscore_raw())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_quality_dip_scores(_quality(), _rsi_raw(), _zscore_raw())
    for d in DATES:
        s = r.loc[r["date"] == d, "quality_dip_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Contrarian direction: low RSI boosts, not penalises ───────────────────────

def test_oversold_boosts_score():
    """A ticker with high quality AND low RSI (oversold) should outscore
    the same quality ticker with a high RSI (overbought)."""
    # A and E both have middling quality; A is oversold (RSI=25), E is overbought (RSI=75)
    qual = _make("quality_score", {DATES[0]: [0.0, 0.0, 0.0, 0.0, 0.0]})  # all equal (at cross-sectional mean)
    rsi = _make("rsi_14_raw", {DATES[0]: [25, 40, 50, 60, 75]})     # A oversold, E overbought
    zsc = _make("rolling_zscore_252d_raw", {DATES[0]: [-2.0, -1.0, 0.0, 0.5, 1.0]})
    r = compute_quality_dip_scores(qual, rsi, zsc)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["quality_dip_score"]
    assert scores["A"] > scores["E"]


def test_high_quality_plus_oversold_top():
    """Highest quality + most oversold = highest QD score."""
    qual = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # A = highest quality
    rsi = _make("rsi_14_raw", {DATES[0]: [20, 30, 50, 60, 70]})       # A = most oversold
    zsc = _make("rolling_zscore_252d_raw", {DATES[0]: [-2.0, -1.0, 0.0, 0.5, 1.0]})
    r = compute_quality_dip_scores(qual, rsi, zsc)
    top = r[r["date"] == DATES[0]].nlargest(1, "quality_dip_score")
    assert top.iloc[0]["ticker"] == "A"


def test_high_quality_overbought_middling():
    """High quality but overbought (RSI=80) should NOT necessarily top the composite
    because the RSI and zscore signals drag it down."""
    qual = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # A = highest quality
    rsi = _make("rsi_14_raw", {DATES[0]: [80, 30, 50, 40, 25]})       # A is overbought, E most oversold
    zsc = _make("rolling_zscore_252d_raw", {DATES[0]: [2.0, 0.0, 0.0, 0.0, -2.0]})
    r = compute_quality_dip_scores(qual, rsi, zsc, quality_weight=0.5, rsi_weight=0.3, rolling_zscore_weight=0.2)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["quality_dip_score"]
    # A has quality advantage but technical disadvantage; E has quality disadvantage but technical advantage
    # With 50% quality weight, A should still beat E on quality alone, but not by as much as quality alone
    # The key test: A should NOT top the chart (E's oversold signal drags A down)
    top_ticker = r[r["date"] == DATES[0]].nlargest(1, "quality_dip_score").iloc[0]["ticker"]
    assert top_ticker != "A"  # overbought highest-quality stock shouldn't top the composite


def test_pure_quality_weight():
    """With quality_weight=1, composite rank == quality rank."""
    qual = _make("quality_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    rsi = _make("rsi_14_raw", {DATES[0]: [70, 60, 50, 40, 20]})  # would favour E
    zsc = _make("rolling_zscore_252d_raw", {DATES[0]: [1.5, 0.5, 0.0, -0.5, -1.5]})  # would favour E
    r = compute_quality_dip_scores(
        qual, rsi, zsc,
        quality_weight=1.0, rsi_weight=0.0, rolling_zscore_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("quality_dip_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Raw values preserved in output ────────────────────────────────────────────

def test_raw_rsi_values_preserved():
    """Original rsi_14_raw values must be in the output, not negated."""
    rsi_vals = [25.0, 40.0, 50.0, 65.0, 75.0]
    rsi = _make("rsi_14_raw", {DATES[0]: rsi_vals})
    qual = _make("quality_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    zsc = _make("rolling_zscore_252d_raw", {DATES[0]: [0.0] * 5})
    r = compute_quality_dip_scores(qual, rsi, zsc)
    d = r[r["date"] == DATES[0]].set_index("ticker")
    for ticker, expected in zip(TICKERS, rsi_vals):
        assert abs(d.loc[ticker, "rsi_14_raw"] - expected) < 1e-9


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_rsi_falls_back():
    qual = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    rsi_rows = [
        {"ticker": t, "date": DATES[0], "rsi_14_raw": np.nan if t == "A" else 50.0}
        for t in TICKERS
    ]
    rsi = pd.DataFrame(rsi_rows)
    zsc = _make("rolling_zscore_252d_raw", {DATES[0]: [0.0] * 5})
    r = compute_quality_dip_scores(qual, rsi, zsc)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["quality_dip_score"])


def test_all_missing_dropped():
    qual = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quality_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quality_score": 1.0},
    ])
    rsi = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "rsi_14_raw": np.nan},
        {"ticker": "B", "date": DATES[0], "rsi_14_raw": 50.0},
    ])
    zsc = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "rolling_zscore_252d_raw": np.nan},
        {"ticker": "B", "date": DATES[0], "rolling_zscore_252d_raw": 0.0},
    ])
    r = compute_quality_dip_scores(qual, rsi, zsc)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_quality_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="quality_scores missing"):
        compute_quality_dip_scores(bad, _rsi_raw(), _zscore_raw())


def test_missing_rsi_raw_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="rsi_raw_scores missing"):
        compute_quality_dip_scores(_quality(), bad, _zscore_raw())


def test_missing_rolling_zscore_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="rolling_zscore_raw_scores missing"):
        compute_quality_dip_scores(_quality(), _rsi_raw(), bad)

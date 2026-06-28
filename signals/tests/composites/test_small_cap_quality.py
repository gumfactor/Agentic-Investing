"""Tests for signals/composites/small_cap_quality.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.small_cap_quality import compute_small_cap_quality_scores


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


def _mktcap(vbd=None):
    # log_market_cap_score is pre-negated: higher = smaller firm
    return _make("log_market_cap_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _quality(vbd=None):
    return _make("quality_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _vol(vbd=None):
    # realized_vol_21d_score: higher = more volatile (negated internally)
    return _make("realized_vol_21d_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_small_cap_quality_scores(_mktcap(), _quality(), _vol())
    assert set(r.columns) >= {
        "ticker", "date",
        "log_market_cap_score", "quality_score", "realized_vol_21d_score",
        "small_cap_quality_score",
    }
    assert "_low_vol" not in r.columns


def test_output_shape():
    r = compute_small_cap_quality_scores(_mktcap(), _quality(), _vol())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_small_cap_quality_scores(_mktcap(), _quality(), _vol())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_small_cap_quality_scores(_mktcap(), _quality(), _vol())
    for d in DATES:
        s = r.loc[r["date"] == d, "small_cap_quality_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Directional correctness: vol negation ────────────────────────────────────

def test_low_vol_boosts_score():
    """Lower realized_vol_21d_score (less volatile) should boost composite
    score, given equal size and quality signals."""
    mktcap = _make("log_market_cap_score", {DATES[0]: _z([3, 3, 3, 3, 3])})
    qual = _make("quality_score", {DATES[0]: _z([3, 3, 3, 3, 3])})
    vol = _make("realized_vol_21d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})  # A = lowest vol
    r = compute_small_cap_quality_scores(mktcap, qual, vol)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["small_cap_quality_score"]
    assert scores["A"] > scores["E"]


def test_raw_vol_score_preserved():
    """Original realized_vol_21d_score must appear in output, not negated."""
    vol_vals = _z([1.0, 2.0, 3.0, 4.0, 5.0])
    vol = _make("realized_vol_21d_score", {DATES[0]: vol_vals})
    r = compute_small_cap_quality_scores(_mktcap(), _quality(), vol)
    d = r[r["date"] == DATES[0]].set_index("ticker")
    for ticker, expected in zip(TICKERS, vol_vals):
        assert abs(d.loc[ticker, "realized_vol_21d_score"] - expected) < 1e-9


def test_smallest_highest_quality_lowest_vol_tops():
    """Smallest cap + highest quality + lowest vol = top composite."""
    mktcap = _make("log_market_cap_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # A = smallest
    qual = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})           # A = best quality
    vol = _make("realized_vol_21d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})   # A = lowest vol
    r = compute_small_cap_quality_scores(mktcap, qual, vol)
    top = r[r["date"] == DATES[0]].nlargest(1, "small_cap_quality_score")
    assert top.iloc[0]["ticker"] == "A"


def test_largest_lowest_quality_highest_vol_bottom():
    """Largest cap + lowest quality + highest vol = bottom composite."""
    mktcap = _make("log_market_cap_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # E = largest
    qual = _make("quality_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    vol = _make("realized_vol_21d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})   # E = highest vol
    r = compute_small_cap_quality_scores(mktcap, qual, vol)
    bot = r[r["date"] == DATES[0]].nsmallest(1, "small_cap_quality_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_pure_market_cap_weight():
    """With market_cap_weight=1, rank == market cap rank (pre-negated)."""
    mktcap = _make("log_market_cap_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_small_cap_quality_scores(
        mktcap, _quality(oppose), _vol(oppose),
        market_cap_weight=1.0, quality_weight=0.0, realized_vol_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("small_cap_quality_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_vol_falls_back():
    vol_rows = [
        {"ticker": t, "date": DATES[0], "realized_vol_21d_score": np.nan if t == "C" else 0.3}
        for t in TICKERS
    ]
    vol = pd.DataFrame(vol_rows)
    r = compute_small_cap_quality_scores(_mktcap(), _quality(), vol)
    c = r[(r["ticker"] == "C") & (r["date"] == DATES[0])]
    assert len(c) == 1
    assert not pd.isna(c.iloc[0]["small_cap_quality_score"])


def test_all_missing_dropped():
    mc = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "log_market_cap_score": np.nan},
        {"ticker": "B", "date": DATES[0], "log_market_cap_score": 1.0},
    ])
    q = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "quality_score": np.nan},
        {"ticker": "B", "date": DATES[0], "quality_score": 0.5},
    ])
    v = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "realized_vol_21d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "realized_vol_21d_score": 0.3},
    ])
    r = compute_small_cap_quality_scores(mc, q, v)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_market_cap_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="market_cap_scores missing"):
        compute_small_cap_quality_scores(bad, _quality(), _vol())


def test_missing_quality_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="quality_scores missing"):
        compute_small_cap_quality_scores(_mktcap(), bad, _vol())


def test_missing_vol_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="realized_vol_scores missing"):
        compute_small_cap_quality_scores(_mktcap(), _quality(), bad)

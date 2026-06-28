"""Tests for signals/composites/short_term_reversal.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.short_term_reversal import compute_short_term_reversal_scores


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


def _rev_1m(vbd=None):
    # Higher = larger recent 1m loss = stronger reversal candidate
    return _make("reversal_1m_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _rev_1w(vbd=None):
    # Higher = larger recent 1w loss = stronger reversal candidate
    return _make("reversal_1w_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _bb_z(vbd=None):
    # Higher raw = price more above its 20-day mean (negated internally for reversal)
    return _make("bb_z_score_20_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_short_term_reversal_scores(_rev_1m(), _rev_1w(), _bb_z())
    assert set(r.columns) >= {
        "ticker", "date",
        "reversal_1m_score", "reversal_1w_score", "bb_z_score_20_score",
        "short_term_reversal_score",
    }
    assert "_below_mean" not in r.columns


def test_output_shape():
    r = compute_short_term_reversal_scores(_rev_1m(), _rev_1w(), _bb_z())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_short_term_reversal_scores(_rev_1m(), _rev_1w(), _bb_z())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_short_term_reversal_scores(_rev_1m(), _rev_1w(), _bb_z())
    for d in DATES:
        s = r.loc[r["date"] == d, "short_term_reversal_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Contrarian direction: bb_z_score negation ─────────────────────────────────

def test_below_mean_boosts_score():
    """Lower bb_z_score (price further below 20-day mean) should boost the
    composite score, given equal reversal signals."""
    rev1m = _make("reversal_1m_score", {DATES[0]: _z([3, 3, 3, 3, 3])})   # all equal
    rev1w = _make("reversal_1w_score", {DATES[0]: _z([3, 3, 3, 3, 3])})   # all equal
    bb = _make("bb_z_score_20_score", {DATES[0]: _z([1, 2, 3, 4, 5])})    # A = most below mean
    r = compute_short_term_reversal_scores(rev1m, rev1w, bb)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["short_term_reversal_score"]
    assert scores["A"] > scores["E"]


def test_biggest_loser_plus_below_mean_tops():
    """Largest 1m and 1w losses + most below 20-day mean = top composite."""
    rev1m = _make("reversal_1m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # A = biggest loser
    rev1w = _make("reversal_1w_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # A = biggest loser
    bb = _make("bb_z_score_20_score", {DATES[0]: _z([1, 2, 3, 4, 5])})    # A = most below mean
    r = compute_short_term_reversal_scores(rev1m, rev1w, bb)
    top = r[r["date"] == DATES[0]].nlargest(1, "short_term_reversal_score")
    assert top.iloc[0]["ticker"] == "A"


def test_recent_winner_above_mean_bottom():
    """Smallest reversal scores + highest bb_z_score = bottom composite."""
    rev1m = _make("reversal_1m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # E = weakest reversal
    rev1w = _make("reversal_1w_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # E = weakest
    bb = _make("bb_z_score_20_score", {DATES[0]: _z([1, 2, 3, 4, 5])})    # E = most above mean
    r = compute_short_term_reversal_scores(rev1m, rev1w, bb)
    bot = r[r["date"] == DATES[0]].nsmallest(1, "short_term_reversal_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_pure_1m_reversal_weight():
    """With reversal_1m_weight=1, rank == 1-month reversal rank."""
    rev1m = _make("reversal_1m_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    bb_oppose = _make("bb_z_score_20_score", {DATES[0]: _z([1, 2, 3, 4, 5])})  # would favour A if used
    r = compute_short_term_reversal_scores(
        rev1m, _rev_1w(oppose), bb_oppose,
        reversal_1m_weight=1.0, reversal_1w_weight=0.0, bb_z_score_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("short_term_reversal_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Raw bb_z_score preserved in output ────────────────────────────────────────

def test_raw_bb_z_score_preserved():
    """Original bb_z_score_20_score must appear in the output, not negated."""
    bb_vals = _z([1.0, 2.0, 3.0, 4.0, 5.0])
    bb = _make("bb_z_score_20_score", {DATES[0]: bb_vals})
    rev1m = _make("reversal_1m_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    rev1w = _make("reversal_1w_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    r = compute_short_term_reversal_scores(rev1m, rev1w, bb)
    d = r[r["date"] == DATES[0]].set_index("ticker")
    for ticker, expected in zip(TICKERS, bb_vals):
        assert abs(d.loc[ticker, "bb_z_score_20_score"] - expected) < 1e-9


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_bb_z_falls_back():
    bb_rows = [
        {"ticker": t, "date": DATES[0], "bb_z_score_20_score": np.nan if t == "B" else -0.5}
        for t in TICKERS
    ]
    bb = pd.DataFrame(bb_rows)
    r = compute_short_term_reversal_scores(_rev_1m(), _rev_1w(), bb)
    b = r[(r["ticker"] == "B") & (r["date"] == DATES[0])]
    assert len(b) == 1
    assert not pd.isna(b.iloc[0]["short_term_reversal_score"])


def test_all_missing_dropped():
    r1m = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "reversal_1m_score": np.nan},
        {"ticker": "B", "date": DATES[0], "reversal_1m_score": 1.0},
    ])
    r1w = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "reversal_1w_score": np.nan},
        {"ticker": "B", "date": DATES[0], "reversal_1w_score": 0.5},
    ])
    bb = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "bb_z_score_20_score": np.nan},
        {"ticker": "B", "date": DATES[0], "bb_z_score_20_score": -0.3},
    ])
    r = compute_short_term_reversal_scores(r1m, r1w, bb)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_reversal_1m_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="reversal_1m_scores missing"):
        compute_short_term_reversal_scores(bad, _rev_1w(), _bb_z())


def test_missing_reversal_1w_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="reversal_1w_scores missing"):
        compute_short_term_reversal_scores(_rev_1m(), bad, _bb_z())


def test_missing_bb_z_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="bb_z_score_scores missing"):
        compute_short_term_reversal_scores(_rev_1m(), _rev_1w(), bad)

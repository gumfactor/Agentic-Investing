"""Tests for signals/composites/low_vol_momentum.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.low_vol_momentum import compute_low_vol_momentum_scores


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


def _vol_adj_mom(vbd=None):
    return _make("vol_adjusted_mom_12m_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _realized_vol(vbd=None):
    # Lower z-score = less volatile = favoured by low-vol strategy
    return _make("realized_vol_21d_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})


def _sortino(vbd=None):
    return _make("sortino_ratio_63d_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_low_vol_momentum_scores(_vol_adj_mom(), _realized_vol(), _sortino())
    assert set(r.columns) >= {
        "ticker", "date",
        "vol_adjusted_mom_12m_score", "realized_vol_21d_score", "sortino_ratio_63d_score",
        "low_vol_momentum_score",
    }
    assert "_low_vol" not in r.columns


def test_output_shape():
    r = compute_low_vol_momentum_scores(_vol_adj_mom(), _realized_vol(), _sortino())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_low_vol_momentum_scores(_vol_adj_mom(), _realized_vol(), _sortino())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_low_vol_momentum_scores(_vol_adj_mom(), _realized_vol(), _sortino())
    for d in DATES:
        s = r.loc[r["date"] == d, "low_vol_momentum_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Contrarian direction: low vol boosts score ─────────────────────────────────

def test_low_vol_boosts_score():
    """A ticker with lower realized vol should score higher than one with
    higher vol, given equal vol-adjusted momentum and Sortino."""
    mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([3, 3, 3, 3, 3])})  # all equal
    sortino = _make("sortino_ratio_63d_score", {DATES[0]: _z([3, 3, 3, 3, 3])})  # all equal
    vol = _make("realized_vol_21d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})  # A = least volatile
    r = compute_low_vol_momentum_scores(mom, vol, sortino)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["low_vol_momentum_score"]
    assert scores["A"] > scores["E"]


def test_high_mom_low_vol_top():
    """Highest vol-adj momentum + lowest realized vol + best Sortino = top composite."""
    mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # A = best
    vol = _make("realized_vol_21d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})        # A = least volatile
    sortino = _make("sortino_ratio_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # A = best
    r = compute_low_vol_momentum_scores(mom, vol, sortino)
    top = r[r["date"] == DATES[0]].nlargest(1, "low_vol_momentum_score")
    assert top.iloc[0]["ticker"] == "A"


def test_low_mom_high_vol_bottom():
    """Lowest vol-adj momentum + highest realized vol + worst Sortino = bottom."""
    mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # E = worst
    vol = _make("realized_vol_21d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})        # E = most volatile
    sortino = _make("sortino_ratio_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})   # E = worst
    r = compute_low_vol_momentum_scores(mom, vol, sortino)
    bot = r[r["date"] == DATES[0]].nsmallest(1, "low_vol_momentum_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_pure_vol_adj_mom_weight():
    """With vol_adj_mom_weight=1, composite rank == vol-adj-mom rank."""
    mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    vol = _make("realized_vol_21d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})    # would favour E
    sortino = _make("sortino_ratio_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # would favour E
    r = compute_low_vol_momentum_scores(
        mom, vol, sortino,
        vol_adj_mom_weight=1.0, realized_vol_weight=0.0, sortino_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("low_vol_momentum_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Raw values preserved in output ────────────────────────────────────────────

def test_raw_realized_vol_preserved():
    """Original realized_vol_21d_score must be in the output, not negated."""
    vol_vals = _z([1.0, 2.0, 3.0, 4.0, 5.0])
    vol = _make("realized_vol_21d_score", {DATES[0]: vol_vals})
    mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    sortino = _make("sortino_ratio_63d_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    r = compute_low_vol_momentum_scores(mom, vol, sortino)
    d = r[r["date"] == DATES[0]].set_index("ticker")
    for ticker, expected in zip(TICKERS, vol_vals):
        assert abs(d.loc[ticker, "realized_vol_21d_score"] - expected) < 1e-9


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_sortino_falls_back():
    mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    vol = _make("realized_vol_21d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    sortino_rows = [
        {"ticker": t, "date": DATES[0], "sortino_ratio_63d_score": np.nan if t == "A" else 0.5}
        for t in TICKERS
    ]
    sortino = pd.DataFrame(sortino_rows)
    r = compute_low_vol_momentum_scores(mom, vol, sortino)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["low_vol_momentum_score"])


def test_all_missing_dropped():
    mom = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "vol_adjusted_mom_12m_score": np.nan},
        {"ticker": "B", "date": DATES[0], "vol_adjusted_mom_12m_score": 1.0},
    ])
    vol = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "realized_vol_21d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "realized_vol_21d_score": 0.5},
    ])
    sortino = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "sortino_ratio_63d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "sortino_ratio_63d_score": 0.5},
    ])
    r = compute_low_vol_momentum_scores(mom, vol, sortino)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_vol_adj_mom_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="vol_adj_mom_scores missing"):
        compute_low_vol_momentum_scores(bad, _realized_vol(), _sortino())


def test_missing_realized_vol_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="realized_vol_scores missing"):
        compute_low_vol_momentum_scores(_vol_adj_mom(), bad, _sortino())


def test_missing_sortino_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="sortino_scores missing"):
        compute_low_vol_momentum_scores(_vol_adj_mom(), _realized_vol(), bad)

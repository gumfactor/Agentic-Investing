"""Tests for signals/composites/risk_adjusted_value.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.risk_adjusted_value import compute_risk_adjusted_value_scores


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
    return _make("value_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _sharpe(vbd=None):
    return _make("sharpe_ratio_252d_score", vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES})


def _drawdown(vbd=None):
    # Lower z-score = smaller drawdown = favoured
    return _make("max_drawdown_63d_score", vbd or {d: _z([1, 2, 3, 4, 5]) for d in DATES})


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_risk_adjusted_value_scores(_value(), _sharpe(), _drawdown())
    assert set(r.columns) >= {
        "ticker", "date",
        "value_score", "sharpe_ratio_252d_score", "max_drawdown_63d_score",
        "risk_adjusted_value_score",
    }
    assert "_drawdown_protected" not in r.columns


def test_output_shape():
    r = compute_risk_adjusted_value_scores(_value(), _sharpe(), _drawdown())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_risk_adjusted_value_scores(_value(), _sharpe(), _drawdown())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_risk_adjusted_value_scores(_value(), _sharpe(), _drawdown())
    for d in DATES:
        s = r.loc[r["date"] == d, "risk_adjusted_value_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Contrarian direction: low drawdown boosts score ───────────────────────────

def test_low_drawdown_boosts_score():
    """A ticker with a lower drawdown score (less severe drawdown) should
    score higher than one with a higher drawdown score, given equal value
    and Sharpe."""
    val = _make("value_score", {DATES[0]: _z([3, 3, 3, 3, 3])})           # all equal
    sharpe = _make("sharpe_ratio_252d_score", {DATES[0]: _z([3, 3, 3, 3, 3])})  # all equal
    dd = _make("max_drawdown_63d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})  # A = smallest drawdown
    r = compute_risk_adjusted_value_scores(val, sharpe, dd)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["risk_adjusted_value_score"]
    assert scores["A"] > scores["E"]


def test_cheap_high_sharpe_low_drawdown_top():
    """Best value + best Sharpe + lowest drawdown = top composite."""
    val = _make("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})              # A = cheapest
    sharpe = _make("sharpe_ratio_252d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # A = best
    dd = _make("max_drawdown_63d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})    # A = smallest
    r = compute_risk_adjusted_value_scores(val, sharpe, dd)
    top = r[r["date"] == DATES[0]].nlargest(1, "risk_adjusted_value_score")
    assert top.iloc[0]["ticker"] == "A"


def test_expensive_low_sharpe_high_drawdown_bottom():
    """Most expensive + worst Sharpe + highest drawdown = bottom."""
    val = _make("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})              # E = most expensive
    sharpe = _make("sharpe_ratio_252d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # E = worst
    dd = _make("max_drawdown_63d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})    # E = highest
    r = compute_risk_adjusted_value_scores(val, sharpe, dd)
    bot = r[r["date"] == DATES[0]].nsmallest(1, "risk_adjusted_value_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_pure_value_weight():
    """With value_weight=1, composite rank == value rank."""
    val = _make("value_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    sharpe = _make("sharpe_ratio_252d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})  # would favour E
    dd = _make("max_drawdown_63d_score", {DATES[0]: _z([5, 4, 3, 2, 1])})    # would favour E
    r = compute_risk_adjusted_value_scores(
        val, sharpe, dd,
        value_weight=1.0, sharpe_weight=0.0, drawdown_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("risk_adjusted_value_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Raw values preserved in output ────────────────────────────────────────────

def test_raw_drawdown_values_preserved():
    """Original max_drawdown_63d_score must be in the output, not negated."""
    dd_vals = _z([1.0, 2.0, 3.0, 4.0, 5.0])
    dd = _make("max_drawdown_63d_score", {DATES[0]: dd_vals})
    val = _make("value_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    sharpe = _make("sharpe_ratio_252d_score", {DATES[0]: _z([3, 4, 2, 5, 1])})
    r = compute_risk_adjusted_value_scores(val, sharpe, dd)
    d = r[r["date"] == DATES[0]].set_index("ticker")
    for ticker, expected in zip(TICKERS, dd_vals):
        assert abs(d.loc[ticker, "max_drawdown_63d_score"] - expected) < 1e-9


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_sharpe_falls_back():
    val = _make("value_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    sharpe_rows = [
        {"ticker": t, "date": DATES[0], "sharpe_ratio_252d_score": np.nan if t == "A" else 0.5}
        for t in TICKERS
    ]
    sharpe = pd.DataFrame(sharpe_rows)
    dd = _make("max_drawdown_63d_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    r = compute_risk_adjusted_value_scores(val, sharpe, dd)
    a = r[(r["ticker"] == "A") & (r["date"] == DATES[0])]
    assert len(a) == 1
    assert not pd.isna(a.iloc[0]["risk_adjusted_value_score"])


def test_all_missing_dropped():
    val = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "value_score": np.nan},
        {"ticker": "B", "date": DATES[0], "value_score": 1.0},
    ])
    sharpe = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "sharpe_ratio_252d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "sharpe_ratio_252d_score": 0.5},
    ])
    dd = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "max_drawdown_63d_score": np.nan},
        {"ticker": "B", "date": DATES[0], "max_drawdown_63d_score": 0.5},
    ])
    r = compute_risk_adjusted_value_scores(val, sharpe, dd)
    assert "A" not in r["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    val = pd.DataFrame([{"ticker": "A", "date": DATES[0], "value_score": 1.0}])
    sharpe = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "sharpe_ratio_252d_score": 0.5},
        {"ticker": "B", "date": DATES[0], "sharpe_ratio_252d_score": 1.0},
    ])
    dd = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "max_drawdown_63d_score": 0.3},
        {"ticker": "B", "date": DATES[0], "max_drawdown_63d_score": 0.1},
    ])
    r = compute_risk_adjusted_value_scores(val, sharpe, dd)
    assert "B" in r["ticker"].values
    b = r[(r["ticker"] == "B") & (r["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["risk_adjusted_value_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_value_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="value_scores missing"):
        compute_risk_adjusted_value_scores(bad, _sharpe(), _drawdown())


def test_missing_sharpe_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="sharpe_scores missing"):
        compute_risk_adjusted_value_scores(_value(), bad, _drawdown())


def test_missing_drawdown_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="drawdown_scores missing"):
        compute_risk_adjusted_value_scores(_value(), _sharpe(), bad)

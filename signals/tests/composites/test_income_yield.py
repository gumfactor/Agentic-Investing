"""Tests for signals/composites/income_yield.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.income_yield import compute_income_yield_scores


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


def _sh_yield(vbd=None):
    return _make(
        "shareholder_yield_score",
        vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES},
    )


def _div_yield(vbd=None):
    return _make(
        "dividend_yield_score",
        vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES},
    )


def _bb_yield(vbd=None):
    return _make(
        "buyback_yield_score",
        vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES},
    )


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_income_yield_scores(_sh_yield(), _div_yield(), _bb_yield())
    assert set(r.columns) >= {
        "ticker", "date",
        "shareholder_yield_score", "dividend_yield_score", "buyback_yield_score",
        "income_yield_score",
    }


def test_output_shape():
    r = compute_income_yield_scores(_sh_yield(), _div_yield(), _bb_yield())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_income_yield_scores(_sh_yield(), _div_yield(), _bb_yield())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_income_yield_scores(_sh_yield(), _div_yield(), _bb_yield())
    for d in DATES:
        s = r.loc[r["date"] == d, "income_yield_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Directional correctness ───────────────────────────────────────────────────

def test_highest_yield_across_all_tops():
    """Highest shareholder yield + dividend yield + buyback yield = top composite."""
    best = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_income_yield_scores(_sh_yield(best), _div_yield(best), _bb_yield(best))
    top = r[r["date"] == DATES[0]].nlargest(1, "income_yield_score")
    assert top.iloc[0]["ticker"] == "A"


def test_lowest_yield_across_all_bottom():
    """Lowest scores on all three = bottom composite."""
    worst = {DATES[0]: _z([5, 4, 3, 2, 1])}  # E = lowest
    r = compute_income_yield_scores(_sh_yield(worst), _div_yield(worst), _bb_yield(worst))
    bot = r[r["date"] == DATES[0]].nsmallest(1, "income_yield_score")
    assert bot.iloc[0]["ticker"] == "E"


def test_high_shareholder_yield_boosts_score():
    """Equal dividend and buyback: higher shareholder yield → higher composite."""
    shy = _make("shareholder_yield_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    equal = {DATES[0]: _z([3, 3, 3, 3, 3])}
    r = compute_income_yield_scores(shy, _div_yield(equal), _bb_yield(equal))
    scores = r[r["date"] == DATES[0]].set_index("ticker")["income_yield_score"]
    assert scores["A"] > scores["E"]


def test_high_dividend_yield_boosts_score():
    """Equal shareholder and buyback: higher dividend yield → higher composite."""
    divy = _make("dividend_yield_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    equal = {DATES[0]: _z([3, 3, 3, 3, 3])}
    r = compute_income_yield_scores(_sh_yield(equal), divy, _bb_yield(equal))
    scores = r[r["date"] == DATES[0]].set_index("ticker")["income_yield_score"]
    assert scores["A"] > scores["E"]


def test_pure_shareholder_yield_weight():
    """With shareholder_yield_weight=1, rank == shareholder yield rank."""
    shy = _make("shareholder_yield_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_income_yield_scores(
        shy, _div_yield(oppose), _bb_yield(oppose),
        shareholder_yield_weight=1.0, dividend_yield_weight=0.0, buyback_yield_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("income_yield_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


def test_pure_dividend_yield_weight():
    """With dividend_yield_weight=1, rank == dividend yield rank."""
    divy = _make("dividend_yield_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_income_yield_scores(
        _sh_yield(oppose), divy, _bb_yield(oppose),
        shareholder_yield_weight=0.0, dividend_yield_weight=1.0, buyback_yield_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("income_yield_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_buyback_yield_falls_back():
    bb_rows = [
        {"ticker": t, "date": DATES[0], "buyback_yield_score": np.nan if t == "B" else 0.5}
        for t in TICKERS
    ]
    bb = pd.DataFrame(bb_rows)
    r = compute_income_yield_scores(_sh_yield(), _div_yield(), bb)
    b = r[(r["ticker"] == "B") & (r["date"] == DATES[0])]
    assert len(b) == 1
    assert not pd.isna(b.iloc[0]["income_yield_score"])


def test_all_missing_dropped():
    sy = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "shareholder_yield_score": np.nan},
        {"ticker": "B", "date": DATES[0], "shareholder_yield_score": 1.0},
    ])
    dy = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "dividend_yield_score": np.nan},
        {"ticker": "B", "date": DATES[0], "dividend_yield_score": 0.5},
    ])
    by = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "buyback_yield_score": np.nan},
        {"ticker": "B", "date": DATES[0], "buyback_yield_score": 0.3},
    ])
    r = compute_income_yield_scores(sy, dy, by)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_shareholder_yield_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="shareholder_yield_scores missing"):
        compute_income_yield_scores(bad, _div_yield(), _bb_yield())


def test_missing_dividend_yield_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="dividend_yield_scores missing"):
        compute_income_yield_scores(_sh_yield(), bad, _bb_yield())


def test_missing_buyback_yield_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="buyback_yield_scores missing"):
        compute_income_yield_scores(_sh_yield(), _div_yield(), bad)

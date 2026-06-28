"""Tests for signals/composites/small_cap_momentum.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.small_cap_momentum import compute_small_cap_momentum_scores


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


def _vol_mom(vbd=None):
    return _make(
        "vol_adjusted_mom_12m_score",
        vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES},
    )


def _rs_12m(vbd=None):
    return _make(
        "rel_strength_vs_spy_12m_score",
        vbd or {d: _z([3, 5, 1, 4, 2]) for d in DATES},
    )


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    r = compute_small_cap_momentum_scores(_mktcap(), _vol_mom(), _rs_12m())
    assert set(r.columns) >= {
        "ticker", "date",
        "log_market_cap_score", "vol_adjusted_mom_12m_score",
        "rel_strength_vs_spy_12m_score", "small_cap_momentum_score",
    }


def test_output_shape():
    r = compute_small_cap_momentum_scores(_mktcap(), _vol_mom(), _rs_12m())
    assert len(r) == len(TICKERS) * len(DATES)


def test_output_sorted():
    r = compute_small_cap_momentum_scores(_mktcap(), _vol_mom(), _rs_12m())
    assert list(r["date"]) == sorted(r["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    r = compute_small_cap_momentum_scores(_mktcap(), _vol_mom(), _rs_12m())
    for d in DATES:
        s = r.loc[r["date"] == d, "small_cap_momentum_score"].dropna()
        assert abs(s.mean()) < 1e-10
        assert abs(s.std(ddof=1) - 1.0) < 1e-10


# ── Directional correctness ───────────────────────────────────────────────────

def test_smallest_best_momentum_tops():
    """Smallest cap + best vol-adj momentum + best relative strength = top."""
    best = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_small_cap_momentum_scores(
        _mktcap(best), _vol_mom(best), _rs_12m(best)
    )
    top = r[r["date"] == DATES[0]].nlargest(1, "small_cap_momentum_score")
    assert top.iloc[0]["ticker"] == "A"


def test_strong_vol_mom_boosts_score():
    """Equal size and relative strength: stronger vol-adj momentum → higher score."""
    vol_mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    equal = {DATES[0]: _z([3, 3, 3, 3, 3])}
    r = compute_small_cap_momentum_scores(_mktcap(equal), vol_mom, _rs_12m(equal))
    scores = r[r["date"] == DATES[0]].set_index("ticker")["small_cap_momentum_score"]
    assert scores["A"] > scores["E"]


def test_strong_rs_boosts_score():
    """Equal size and vol-adj momentum: stronger relative strength → higher score."""
    rs = _make("rel_strength_vs_spy_12m_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    equal = {DATES[0]: _z([3, 3, 3, 3, 3])}
    r = compute_small_cap_momentum_scores(_mktcap(equal), _vol_mom(equal), rs)
    scores = r[r["date"] == DATES[0]].set_index("ticker")["small_cap_momentum_score"]
    assert scores["A"] > scores["E"]


def test_pure_market_cap_weight():
    """With market_cap_weight=1, rank == pre-negated market cap rank."""
    mktcap = _make("log_market_cap_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_small_cap_momentum_scores(
        mktcap, _vol_mom(oppose), _rs_12m(oppose),
        market_cap_weight=1.0, vol_adj_mom_weight=0.0, rel_strength_12m_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("small_cap_momentum_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


def test_pure_vol_mom_weight():
    """With vol_adj_mom_weight=1, rank == vol-adjusted momentum rank."""
    vol_mom = _make("vol_adjusted_mom_12m_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    oppose = {DATES[0]: _z([5, 4, 3, 2, 1])}
    r = compute_small_cap_momentum_scores(
        _mktcap(oppose), vol_mom, _rs_12m(oppose),
        market_cap_weight=0.0, vol_adj_mom_weight=1.0, rel_strength_12m_weight=0.0,
    )
    d = r[r["date"] == DATES[0]].sort_values("small_cap_momentum_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_rs_falls_back():
    rs_rows = [
        {"ticker": t, "date": DATES[0], "rel_strength_vs_spy_12m_score": np.nan if t == "D" else 0.4}
        for t in TICKERS
    ]
    rs = pd.DataFrame(rs_rows)
    r = compute_small_cap_momentum_scores(_mktcap(), _vol_mom(), rs)
    d_row = r[(r["ticker"] == "D") & (r["date"] == DATES[0])]
    assert len(d_row) == 1
    assert not pd.isna(d_row.iloc[0]["small_cap_momentum_score"])


def test_all_missing_dropped():
    mc = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "log_market_cap_score": np.nan},
        {"ticker": "B", "date": DATES[0], "log_market_cap_score": 1.0},
    ])
    vm = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "vol_adjusted_mom_12m_score": np.nan},
        {"ticker": "B", "date": DATES[0], "vol_adjusted_mom_12m_score": 0.5},
    ])
    rs = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "rel_strength_vs_spy_12m_score": np.nan},
        {"ticker": "B", "date": DATES[0], "rel_strength_vs_spy_12m_score": 0.3},
    ])
    r = compute_small_cap_momentum_scores(mc, vm, rs)
    assert "A" not in r["ticker"].values


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_market_cap_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="market_cap_scores missing"):
        compute_small_cap_momentum_scores(bad, _vol_mom(), _rs_12m())


def test_missing_vol_mom_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="vol_adj_mom_scores missing"):
        compute_small_cap_momentum_scores(_mktcap(), bad, _rs_12m())


def test_missing_rs_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="rel_strength_12m_scores missing"):
        compute_small_cap_momentum_scores(_mktcap(), _vol_mom(), bad)

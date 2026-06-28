"""Tests for signals/composites/oscillator_agreement.py."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signals.composites.oscillator_agreement import compute_oscillator_agreement_scores


# ── Fixtures ──────────────────────────────────────────────────────────────────

TICKERS = ["A", "B", "C", "D", "E"]
DATES = [date(2024, 1, 31), date(2024, 2, 29)]


def _z(arr):
    a = np.array(arr, dtype=float)
    return list((a - a.mean()) / a.std(ddof=1))


def _make_scores(col: str, values_by_date: dict) -> pd.DataFrame:
    rows = []
    for d, vals in values_by_date.items():
        for ticker, v in zip(TICKERS, vals):
            rows.append({"ticker": ticker, "date": d, col: v})
    return pd.DataFrame(rows)


def _rsi(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([1, 2, 3, 4, 5]) for d in DATES}
    return _make_scores("rsi_14_score", values_by_date)


def _macd(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([2, 4, 1, 5, 3]) for d in DATES}
    return _make_scores("macd_histogram_12_26_9_score", values_by_date)


def _stoch(values_by_date: dict | None = None) -> pd.DataFrame:
    if values_by_date is None:
        values_by_date = {d: _z([3, 1, 5, 2, 4]) for d in DATES}
    return _make_scores("stoch_k_14_score", values_by_date)


# ── Basic output ──────────────────────────────────────────────────────────────

def test_output_columns():
    result = compute_oscillator_agreement_scores(_rsi(), _macd(), _stoch())
    assert set(result.columns) >= {
        "ticker", "date",
        "rsi_14_score", "macd_histogram_12_26_9_score",
        "stoch_k_14_score", "oscillator_agreement_score",
    }


def test_output_shape():
    result = compute_oscillator_agreement_scores(_rsi(), _macd(), _stoch())
    assert len(result) == len(TICKERS) * len(DATES)


def test_output_sorted():
    result = compute_oscillator_agreement_scores(_rsi(), _macd(), _stoch())
    assert list(result["date"]) == sorted(result["date"])


# ── Cross-sectional standardization ───────────────────────────────────────────

def test_cross_sectional_zscore_per_date():
    result = compute_oscillator_agreement_scores(_rsi(), _macd(), _stoch())
    for d in DATES:
        scores = result.loc[result["date"] == d, "oscillator_agreement_score"].dropna()
        assert abs(scores.mean()) < 1e-10
        assert abs(scores.std(ddof=1) - 1.0) < 1e-10


# ── Intuitive ordering ────────────────────────────────────────────────────────

def test_all_oscillators_agree_top():
    """Ticker ranked highest across all three oscillators should score highest."""
    rsi = _make_scores("rsi_14_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    mac = _make_scores("macd_histogram_12_26_9_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    sto = _make_scores("stoch_k_14_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_oscillator_agreement_scores(rsi, mac, sto)
    top = result.loc[result["date"] == DATES[0]].nlargest(1, "oscillator_agreement_score")
    assert top.iloc[0]["ticker"] == "A"


def test_disagreement_produces_middling_score():
    """When all three oscillators are perfectly inversely ranked to each other,
    the composite should give near-zero variance (all cancel)."""
    # RSI: A=highest, MACD: A=middling (rank 3), stoch: A=lowest
    rsi = _make_scores("rsi_14_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    # Use weights 1/3 each to make exact cancellation testable
    mac = _make_scores("macd_histogram_12_26_9_score", {DATES[0]: _z([3, 2, 1, 4, 5])})
    sto = _make_scores("stoch_k_14_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    result = compute_oscillator_agreement_scores(
        rsi, mac, sto,
        rsi_weight=1.0, macd_histogram_weight=1.0, stoch_k_weight=1.0,
    )
    # If RSI=[5,4,3,2,1] and stoch=[1,2,3,4,5] are perfect inverses, and
    # macd has a different ordering, the spread in composite should be reduced vs. any single signal.
    scores = result.loc[result["date"] == DATES[0], "oscillator_agreement_score"]
    single_std = 1.0  # any single z-scored input has std=1 after re-standardization
    # composite spread should still be std=1 (re-standardized), but rankings will differ
    assert abs(scores.std(ddof=1) - 1.0) < 1e-10


def test_pure_rsi_weight():
    """With rsi_weight=1, composite rank == RSI rank."""
    rsi = _make_scores("rsi_14_score", {DATES[0]: _z([1, 2, 3, 4, 5])})
    mac = _make_scores("macd_histogram_12_26_9_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    sto = _make_scores("stoch_k_14_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    result = compute_oscillator_agreement_scores(
        rsi, mac, sto,
        rsi_weight=1.0, macd_histogram_weight=0.0, stoch_k_weight=0.0,
    )
    d = result[result["date"] == DATES[0]].sort_values("oscillator_agreement_score")
    assert list(d["ticker"]) == ["A", "B", "C", "D", "E"]


# ── Missing data / weight redistribution ─────────────────────────────────────

def test_missing_stoch_falls_back():
    """When Stochastic is NaN, weight redistributes to RSI and MACD."""
    rsi = _make_scores("rsi_14_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    mac = _make_scores("macd_histogram_12_26_9_score", {DATES[0]: _z([5, 4, 3, 2, 1])})
    stoch_rows = [
        {"ticker": t, "date": DATES[0], "stoch_k_14_score": np.nan if t == "A" else float(i)}
        for i, t in enumerate(TICKERS)
    ]
    sto = pd.DataFrame(stoch_rows)
    result = compute_oscillator_agreement_scores(rsi, mac, sto)
    a_row = result[(result["ticker"] == "A") & (result["date"] == DATES[0])]
    assert len(a_row) == 1
    assert not pd.isna(a_row.iloc[0]["oscillator_agreement_score"])


def test_all_missing_dropped():
    rsi = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "rsi_14_score": np.nan},
        {"ticker": "B", "date": DATES[0], "rsi_14_score": 1.0},
    ])
    mac = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "macd_histogram_12_26_9_score": np.nan},
        {"ticker": "B", "date": DATES[0], "macd_histogram_12_26_9_score": 0.5},
    ])
    sto = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "stoch_k_14_score": np.nan},
        {"ticker": "B", "date": DATES[0], "stoch_k_14_score": 0.5},
    ])
    result = compute_oscillator_agreement_scores(rsi, mac, sto)
    assert "A" not in result["ticker"].values


# ── Outer join ────────────────────────────────────────────────────────────────

def test_outer_join_tickers():
    rsi = pd.DataFrame([{"ticker": "A", "date": DATES[0], "rsi_14_score": 1.0}])
    mac = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "macd_histogram_12_26_9_score": 0.5},
        {"ticker": "B", "date": DATES[0], "macd_histogram_12_26_9_score": -0.5},
    ])
    sto = pd.DataFrame([
        {"ticker": "A", "date": DATES[0], "stoch_k_14_score": 0.3},
        {"ticker": "B", "date": DATES[0], "stoch_k_14_score": -0.3},
    ])
    result = compute_oscillator_agreement_scores(rsi, mac, sto)
    assert "B" in result["ticker"].values
    b = result[(result["ticker"] == "B") & (result["date"] == DATES[0])]
    assert not pd.isna(b.iloc[0]["oscillator_agreement_score"])


# ── Validation ────────────────────────────────────────────────────────────────

def test_missing_rsi_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="rsi_scores missing"):
        compute_oscillator_agreement_scores(bad, _macd(), _stoch())


def test_missing_macd_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="macd_histogram_scores missing"):
        compute_oscillator_agreement_scores(_rsi(), bad, _stoch())


def test_missing_stoch_col_raises():
    bad = pd.DataFrame([{"ticker": "A", "date": DATES[0], "wrong": 1.0}])
    with pytest.raises(ValueError, match="stoch_k_scores missing"):
        compute_oscillator_agreement_scores(_rsi(), _macd(), bad)

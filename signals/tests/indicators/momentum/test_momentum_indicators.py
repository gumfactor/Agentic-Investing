"""Unit tests for signals/indicators/momentum/* factors."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signals.indicators.momentum.returns.mom_1w import compute_mom_1w_scores
from signals.indicators.momentum.returns.mom_2w import compute_mom_2w_scores
from signals.indicators.momentum.returns.mom_1m import compute_mom_1m_scores
from signals.indicators.momentum.returns.mom_3m import compute_mom_3m_scores
from signals.indicators.momentum.returns.mom_6m import compute_mom_6m_scores
from signals.indicators.momentum.returns.mom_12m import compute_mom_12m_scores
from signals.indicators.momentum.returns.mom_24m import compute_mom_24m_scores
from signals.indicators.momentum.returns.mom_36m import compute_mom_36m_scores
from signals.indicators.momentum.breakout.price_vs_52w_high import compute_price_vs_52w_high_scores
from signals.indicators.momentum.breakout.price_vs_52w_low import compute_price_vs_52w_low_scores
from signals.indicators.momentum.breakout.price_vs_4w_high import compute_price_vs_4w_high_scores
from signals.indicators.momentum.breakout.price_vs_4w_low import compute_price_vs_4w_low_scores
from signals.indicators.momentum.breakout.donchian_pct import compute_donchian_pct_scores
from signals.indicators.momentum.relative_strength.rel_strength_vs_spy_12m import compute_rel_strength_vs_spy_12m_scores
from signals.indicators.momentum.relative_strength.rel_strength_vs_spy_3m import compute_rel_strength_vs_spy_3m_scores
from signals.indicators.momentum.reversals.reversal_1w import compute_reversal_1w_scores
from signals.indicators.momentum.reversals.reversal_1m import compute_reversal_1m_scores
from signals.indicators.momentum.reversals.reversal_36m import compute_reversal_36m_scores
from signals.indicators.momentum.trend_quality.drawdown_from_peak_63d import compute_drawdown_from_peak_63d_scores
from signals.indicators.momentum.trend_quality.drawdown_from_peak_252d import compute_drawdown_from_peak_252d_scores
from signals.indicators.momentum.trend_quality.trend_consistency_21d import compute_trend_consistency_21d_scores
from signals.indicators.momentum.trend_quality.trend_consistency_63d import compute_trend_consistency_63d_scores
from signals.indicators.momentum.trend_quality.trend_r2_50d import compute_trend_r2_50d_scores

from signals.tests.indicators.conftest import make_prices, make_prices_with_spy, _latest_scores

# ─── Smoke tests ─────────────────────────────────────────────────────────────

_RETURN_CASES = [
    (compute_mom_1w_scores,  "mom_1w_score"),
    (compute_mom_2w_scores,  "mom_2w_score"),
    (compute_mom_1m_scores,  "mom_1m_score"),
    (compute_mom_3m_scores,  "mom_3m_score"),
    (compute_mom_6m_scores,  "mom_6m_score"),
    (compute_mom_12m_scores, "mom_12m_score"),
]

_BREAKOUT_CASES = [
    (compute_price_vs_52w_high_scores, "price_vs_52w_high_score"),
    (compute_price_vs_52w_low_scores,  "price_vs_52w_low_score"),
    (compute_price_vs_4w_high_scores,  "price_vs_4w_high_score"),
    (compute_price_vs_4w_low_scores,   "price_vs_4w_low_score"),
    (compute_donchian_pct_scores,      "donchian_pct_score"),
]

_REVERSAL_CASES = [
    (compute_reversal_1w_scores, "reversal_1w_score"),
    (compute_reversal_1m_scores, "reversal_1m_score"),
]

_TREND_CASES = [
    (compute_drawdown_from_peak_63d_scores,  "drawdown_from_peak_63d_score"),
    (compute_trend_consistency_21d_scores,   "trend_consistency_21d_score"),
    (compute_trend_consistency_63d_scores,   "trend_consistency_63d_score"),
    (compute_trend_r2_50d_scores,            "trend_r2_50d_score"),
]


@pytest.mark.parametrize("fn,score_col", _RETURN_CASES, ids=[c[1] for c in _RETURN_CASES])
def test_momentum_return_smoke(fn, score_col, prices_300d):
    result = fn(prices_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,score_col", _BREAKOUT_CASES, ids=[c[1] for c in _BREAKOUT_CASES])
def test_momentum_breakout_smoke(fn, score_col, prices_400d):
    result = fn(prices_400d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,score_col", _REVERSAL_CASES, ids=[c[1] for c in _REVERSAL_CASES])
def test_momentum_reversal_smoke(fn, score_col, prices_300d):
    result = fn(prices_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,score_col", _TREND_CASES, ids=[c[1] for c in _TREND_CASES])
def test_momentum_trend_smoke(fn, score_col, prices_300d):
    result = fn(prices_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


def test_mom_24m_smoke(prices_810d):
    result = compute_mom_24m_scores(prices_810d)
    assert {"date", "ticker", "mom_24m_score"} <= set(result.columns)
    assert len(result) > 0


def test_mom_36m_smoke(prices_810d):
    result = compute_mom_36m_scores(prices_810d)
    assert {"date", "ticker", "mom_36m_score"} <= set(result.columns)
    assert len(result) > 0


def test_drawdown_from_peak_252d_smoke(prices_400d):
    result = compute_drawdown_from_peak_252d_scores(prices_400d)
    assert {"date", "ticker", "drawdown_from_peak_252d_score"} <= set(result.columns)
    assert len(result) > 0


def test_reversal_36m_smoke(prices_810d):
    result = compute_reversal_36m_scores(prices_810d)
    assert {"date", "ticker", "reversal_36m_score"} <= set(result.columns)
    assert len(result) > 0


def test_rel_strength_vs_spy_12m_smoke(prices_with_spy_400d):
    result = compute_rel_strength_vs_spy_12m_scores(prices_with_spy_400d)
    assert {"date", "ticker", "rel_strength_vs_spy_12m_score"} <= set(result.columns)
    assert "SPY" not in result["ticker"].values
    assert len(result) > 0


def test_rel_strength_vs_spy_3m_smoke(prices_with_spy_400d):
    result = compute_rel_strength_vs_spy_3m_scores(prices_with_spy_400d)
    assert {"date", "ticker", "rel_strength_vs_spy_3m_score"} <= set(result.columns)
    assert "SPY" not in result["ticker"].values
    assert len(result) > 0


# ─── Behavioral tests ────────────────────────────────────────────────────────

def test_mom_12m_rising_stock_scores_higher():
    """A stock with a strong positive trend should score higher than a flat stock."""
    dates = pd.bdate_range("2020-01-01", periods=400)
    rows = []
    for d, i in zip(dates, range(400)):
        rows.append({"date": d, "ticker": "RISER", "close": 100.0 + i * 0.5})
        rows.append({"date": d, "ticker": "FLAT",  "close": 100.0})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_mom_12m_scores(prices), "mom_12m_score")
    assert scores["RISER"] > scores["FLAT"]


def test_reversal_1m_underperformer_scores_higher():
    """A stock that fell last month should score higher on the reversal indicator."""
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    for d, i in zip(dates, range(100)):
        rows.append({"date": d, "ticker": "LOSER",  "close": 100.0 - i * 0.3})
        rows.append({"date": d, "ticker": "WINNER", "close": 100.0 + i * 0.3})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_reversal_1m_scores(prices), "reversal_1m_score")
    assert scores["LOSER"] > scores["WINNER"]


def test_price_vs_52w_high_near_high_scores_higher():
    """Stock trading at its 52w high should score higher than one far below it."""
    dates = pd.bdate_range("2020-01-01", periods=300)
    rows = []
    for d, i in zip(dates, range(300)):
        rows.append({"date": d, "ticker": "ATHIGH", "close": 100.0})
        rows.append({"date": d, "ticker": "ATLOW",  "close": 50.0 if i > 250 else 100.0})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_price_vs_52w_high_scores(prices), "price_vs_52w_high_score")
    assert scores["ATHIGH"] > scores["ATLOW"]


def test_rel_strength_missing_spy_raises():
    prices = make_prices()
    with pytest.raises(ValueError, match="SPY"):
        compute_rel_strength_vs_spy_12m_scores(prices)


def test_momentum_empty_prices_raises():
    with pytest.raises(ValueError):
        compute_mom_12m_scores(pd.DataFrame(columns=["date", "ticker", "close"]))


def test_mom_6m_rising_stock_scores_higher():
    """Stock with a strong 6-month return should outscore a declining one."""
    dates = pd.bdate_range("2020-01-01", periods=200)
    rows = []
    for d, i in zip(dates, range(200)):
        rows.append({"date": d, "ticker": "UP",   "close": 100.0 + i * 0.4})
        rows.append({"date": d, "ticker": "DOWN", "close": 100.0 - i * 0.2})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_mom_6m_scores(prices), "mom_6m_score")
    assert scores["UP"] > scores["DOWN"]


def test_trend_consistency_21d_consistent_up_scores_higher():
    """Stock with more positive-return days in the past 21 days scores higher."""
    dates = pd.bdate_range("2020-01-01", periods=60)
    rows = []
    for d, i in zip(dates, range(60)):
        rows.append({"date": d, "ticker": "UP",   "close": 100.0 + i * 0.2})
        rows.append({"date": d, "ticker": "DOWN", "close": 100.0 - i * 0.2})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_trend_consistency_21d_scores(prices), "trend_consistency_21d_score")
    assert scores["UP"] > scores["DOWN"]


def test_drawdown_from_peak_63d_at_peak_scores_higher():
    """Stock trading at its 63-day high scores higher than one far below its peak."""
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    for d, i in zip(dates, range(100)):
        rows.append({"date": d, "ticker": "PEAK",   "close": 100.0})
        fallen = 60.0 if i >= 80 else 100.0
        rows.append({"date": d, "ticker": "FALLEN", "close": float(fallen)})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_drawdown_from_peak_63d_scores(prices), "drawdown_from_peak_63d_score")
    assert scores["PEAK"] > scores["FALLEN"]

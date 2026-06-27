"""Unit tests for signals/factors/moving_averages/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.factors.moving_averages.crossovers.ma_cross_5_20 import compute_ma_cross_5_20_scores
from signals.factors.moving_averages.crossovers.ma_cross_20_50 import compute_ma_cross_20_50_scores
from signals.factors.moving_averages.crossovers.ma_cross_50_200 import compute_ma_cross_50_200_scores
from signals.factors.moving_averages.crossovers.ema_cross_12_26 import compute_ema_cross_12_26_scores
from signals.factors.moving_averages.price_vs_ma.price_vs_sma_20 import compute_price_vs_sma_20_scores
from signals.factors.moving_averages.price_vs_ma.price_vs_sma_50 import compute_price_vs_sma_50_scores
from signals.factors.moving_averages.price_vs_ma.price_vs_sma_100 import compute_price_vs_sma_100_scores
from signals.factors.moving_averages.price_vs_ma.price_vs_sma_200 import compute_price_vs_sma_200_scores
from signals.factors.moving_averages.price_vs_ma.price_vs_ema_12 import compute_price_vs_ema_12_scores
from signals.factors.moving_averages.price_vs_ma.price_vs_ema_26 import compute_price_vs_ema_26_scores
from signals.factors.moving_averages.price_vs_ma.price_vs_ema_50 import compute_price_vs_ema_50_scores
from signals.factors.moving_averages.price_vs_ma.price_vs_ema_200 import compute_price_vs_ema_200_scores
from signals.factors.moving_averages.slopes.ma_slope_50 import compute_ma_slope_50_scores
from signals.factors.moving_averages.slopes.ma_slope_200 import compute_ma_slope_200_scores

from signals.tests.factors.conftest import _latest_scores

# ─── Smoke tests ─────────────────────────────────────────────────────────────

_SHORT_WINDOW_CASES = [
    (compute_ma_cross_5_20_scores,   "ma_cross_5_20_score"),
    (compute_ma_cross_20_50_scores,  "ma_cross_20_50_score"),
    (compute_ema_cross_12_26_scores, "ema_cross_12_26_score"),
    (compute_price_vs_sma_20_scores, "price_vs_sma_20_score"),
    (compute_price_vs_sma_50_scores, "price_vs_sma_50_score"),
    (compute_price_vs_ema_12_scores, "price_vs_ema_12_score"),
    (compute_price_vs_ema_26_scores, "price_vs_ema_26_score"),
    (compute_price_vs_ema_50_scores, "price_vs_ema_50_score"),
]

_LONG_WINDOW_CASES = [
    (compute_ma_cross_50_200_scores,  "ma_cross_50_200_score"),
    (compute_price_vs_sma_100_scores, "price_vs_sma_100_score"),
    (compute_price_vs_sma_200_scores, "price_vs_sma_200_score"),
    (compute_price_vs_ema_200_scores, "price_vs_ema_200_score"),
    (compute_ma_slope_50_scores,      "ma_slope_50_score"),
    (compute_ma_slope_200_scores,     "ma_slope_200_score"),
]


@pytest.mark.parametrize("fn,score_col", _SHORT_WINDOW_CASES, ids=[c[1] for c in _SHORT_WINDOW_CASES])
def test_moving_average_short_window_smoke(fn, score_col, prices_300d):
    result = fn(prices_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,score_col", _LONG_WINDOW_CASES, ids=[c[1] for c in _LONG_WINDOW_CASES])
def test_moving_average_long_window_smoke(fn, score_col, prices_400d):
    result = fn(prices_400d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


# ─── Behavioral tests ────────────────────────────────────────────────────────

def test_price_vs_sma_200_above_ma_scores_higher():
    """Stock trading above its 200-day SMA should score higher than one below."""
    dates = pd.bdate_range("2020-01-01", periods=300)
    rows = []
    for d, i in zip(dates, range(300)):
        rows.append({"date": d, "ticker": "ABOVE", "close": 120.0})
        rows.append({"date": d, "ticker": "BELOW", "close": 80.0 if i > 200 else 120.0})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_price_vs_sma_200_scores(prices), "price_vs_sma_200_score")
    assert scores["ABOVE"] > scores["BELOW"]


def test_ma_cross_50_200_golden_cross_scores_higher():
    """A stock in a golden cross (fast > slow MA) should score higher."""
    dates = pd.bdate_range("2020-01-01", periods=400)
    rows = []
    for d, i in zip(dates, range(400)):
        rows.append({"date": d, "ticker": "GOLD",  "close": 100.0 + i * 0.2})
        rows.append({"date": d, "ticker": "DEATH", "close": 100.0 - i * 0.1})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_ma_cross_50_200_scores(prices), "ma_cross_50_200_score")
    assert scores["GOLD"] > scores["DEATH"]


def test_ma_slope_200_rising_trend_scores_higher():
    """A stock with a rising 200-day MA should score higher than a flat one."""
    dates = pd.bdate_range("2020-01-01", periods=400)
    rows = []
    for d, i in zip(dates, range(400)):
        rows.append({"date": d, "ticker": "RISING", "close": 100.0 + i * 0.3})
        rows.append({"date": d, "ticker": "FLAT",   "close": 100.0})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_ma_slope_200_scores(prices), "ma_slope_200_score")
    assert scores["RISING"] > scores["FLAT"]


def test_moving_average_empty_prices_raises():
    with pytest.raises(ValueError):
        compute_price_vs_sma_200_scores(pd.DataFrame(columns=["date", "ticker", "close"]))

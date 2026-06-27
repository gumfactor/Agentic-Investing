"""Unit tests for signals/indicators/volume/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.indicators.volume.volume_trend.volume_ma_ratio_21d import compute_volume_ma_ratio_21d_scores
from signals.indicators.volume.volume_trend.volume_ma_ratio_63d import compute_volume_ma_ratio_63d_scores
from signals.indicators.volume.volume_trend.volume_roc_21d import compute_volume_roc_21d_scores
from signals.indicators.volume.volume_trend.volume_percentile_252d import compute_volume_percentile_252d_scores
from signals.indicators.volume.volume_trend.volume_trend_slope_21d import compute_volume_trend_slope_21d_scores
from signals.indicators.volume.volume_trend.volume_up_down_ratio_21d import compute_volume_up_down_ratio_21d_scores
from signals.indicators.volume.price_volume.obv_momentum_21d import compute_obv_momentum_21d_scores
from signals.indicators.volume.price_volume.obv_momentum_63d import compute_obv_momentum_63d_scores
from signals.indicators.volume.price_volume.price_volume_trend_21d import compute_price_volume_trend_21d_scores
from signals.indicators.volume.price_volume.volume_weighted_momentum_21d import compute_volume_weighted_momentum_21d_scores
from signals.indicators.volume.price_volume.force_index_13d import compute_force_index_13d_scores
from signals.indicators.volume.accumulation.chaikin_money_flow_21d import compute_chaikin_money_flow_21d_scores
from signals.indicators.volume.accumulation.chaikin_oscillator import compute_chaikin_oscillator_scores
from signals.indicators.volume.accumulation.ad_line_momentum_21d import compute_ad_line_momentum_21d_scores
from signals.indicators.volume.accumulation.ease_of_movement_14d import compute_ease_of_movement_14d_scores
from signals.indicators.volume.accumulation.money_flow_index_14d import compute_money_flow_index_14d_scores

from signals.tests.indicators.conftest import make_prices, make_volumes, _latest_scores

# ─── Smoke tests ─────────────────────────────────────────────────────────────

_VOLUME_ONLY_CASES = [
    (compute_volume_ma_ratio_21d_scores,   "volume_ma_ratio_21d_score"),
    (compute_volume_ma_ratio_63d_scores,   "volume_ma_ratio_63d_score"),
    (compute_volume_roc_21d_scores,        "volume_roc_21d_score"),
    (compute_volume_trend_slope_21d_scores, "volume_trend_slope_21d_score"),
]

_PRICES_VOLUMES_CASES = [
    (compute_obv_momentum_21d_scores,              "obv_momentum_21d_score"),
    (compute_obv_momentum_63d_scores,              "obv_momentum_63d_score"),
    (compute_price_volume_trend_21d_scores,        "price_volume_trend_21d_score"),
    (compute_volume_weighted_momentum_21d_scores,  "volume_weighted_momentum_21d_score"),
    (compute_force_index_13d_scores,               "force_index_13d_score"),
    (compute_volume_up_down_ratio_21d_scores,      "volume_up_down_ratio_21d_score"),
]

_OHLCV_CASES = [
    (compute_chaikin_money_flow_21d_scores, "chaikin_money_flow_21d_score"),
    (compute_chaikin_oscillator_scores,     "chaikin_oscillator_score"),
    (compute_ad_line_momentum_21d_scores,   "ad_line_momentum_21d_score"),
    (compute_ease_of_movement_14d_scores,   "ease_of_movement_14d_score"),
    (compute_money_flow_index_14d_scores,   "money_flow_index_14d_score"),
]


@pytest.mark.parametrize("fn,score_col", _VOLUME_ONLY_CASES, ids=[c[1] for c in _VOLUME_ONLY_CASES])
def test_volume_only_smoke(fn, score_col, volumes_300d):
    result = fn(volumes_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,score_col", _PRICES_VOLUMES_CASES, ids=[c[1] for c in _PRICES_VOLUMES_CASES])
def test_prices_volumes_smoke(fn, score_col, prices_300d, volumes_300d):
    result = fn(prices_300d, volumes_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,score_col", _OHLCV_CASES, ids=[c[1] for c in _OHLCV_CASES])
def test_ohlcv_smoke(fn, score_col, ohlcv_300d):
    result = fn(ohlcv_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


def test_volume_percentile_252d_smoke(volumes_400d):
    result = compute_volume_percentile_252d_scores(volumes_400d)
    assert {"date", "ticker", "volume_percentile_252d_score"} <= set(result.columns)
    assert len(result) > 0


# ─── Behavioral tests ────────────────────────────────────────────────────────

def test_volume_ma_ratio_21d_spike_scores_higher():
    """A ticker with an unusual volume spike should score higher than one with flat volume."""
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    for d, i in zip(dates, range(100)):
        spike = 10_000_000.0 if i >= 80 else 1_000_000.0
        rows.append({"date": d, "ticker": "SPIKE", "volume": spike})
        rows.append({"date": d, "ticker": "FLAT",  "volume": 1_000_000.0})
    volumes = pd.DataFrame(rows)
    scores = _latest_scores(compute_volume_ma_ratio_21d_scores(volumes), "volume_ma_ratio_21d_score")
    assert scores["SPIKE"] > scores["FLAT"]


def test_obv_momentum_21d_accumulation_scores_higher():
    """Net accumulation (rising price on high volume) should score higher than distribution."""
    dates = pd.bdate_range("2020-01-01", periods=100)
    price_rows, vol_rows = [], []
    for d, i in zip(dates, range(100)):
        price_rows.append({"date": d, "ticker": "ACCUM", "close": 100.0 + i * 0.5})
        price_rows.append({"date": d, "ticker": "DISTR", "close": 100.0 - i * 0.5})
        vol_rows.append({"date": d, "ticker": "ACCUM", "volume": 2_000_000.0})
        vol_rows.append({"date": d, "ticker": "DISTR", "volume": 2_000_000.0})
    prices = pd.DataFrame(price_rows)
    volumes = pd.DataFrame(vol_rows)
    scores = _latest_scores(compute_obv_momentum_21d_scores(prices, volumes), "obv_momentum_21d_score")
    assert scores["ACCUM"] > scores["DISTR"]


def test_volume_weighted_momentum_up_on_volume_scores_higher():
    """Price rising on high-volume days should outscore flat price."""
    dates = pd.bdate_range("2020-01-01", periods=150)
    price_rows, vol_rows = [], []
    for d, i in zip(dates, range(150)):
        price_rows.append({"date": d, "ticker": "STRONG", "close": 100.0 + i * 0.5})
        price_rows.append({"date": d, "ticker": "WEAK",   "close": 100.0})
        vol_rows.append({"date": d, "ticker": "STRONG", "volume": 2_000_000.0})
        vol_rows.append({"date": d, "ticker": "WEAK",   "volume": 2_000_000.0})
    prices = pd.DataFrame(price_rows)
    volumes = pd.DataFrame(vol_rows)
    scores = _latest_scores(
        compute_volume_weighted_momentum_21d_scores(prices, volumes),
        "volume_weighted_momentum_21d_score",
    )
    assert scores["STRONG"] > scores["WEAK"]


def test_volume_missing_column_raises():
    with pytest.raises(ValueError):
        compute_volume_ma_ratio_21d_scores(pd.DataFrame(columns=["date", "ticker"]))


def test_ohlcv_missing_volume_column_raises():
    with pytest.raises(ValueError):
        compute_chaikin_money_flow_21d_scores(
            pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close"])
        )


def test_volume_roc_21d_rising_volume_scores_higher():
    """Ticker with steadily rising volume over 21 days scores higher than flat volume."""
    dates = pd.bdate_range("2020-01-01", periods=80)
    rows = []
    for d, i in zip(dates, range(80)):
        rows.append({"date": d, "ticker": "RISING", "volume": 1_000_000.0 + i * 20_000.0})
        rows.append({"date": d, "ticker": "FLAT",   "volume": 1_000_000.0})
    volumes = pd.DataFrame(rows)
    scores = _latest_scores(compute_volume_roc_21d_scores(volumes), "volume_roc_21d_score")
    assert scores["RISING"] > scores["FLAT"]


def test_chaikin_money_flow_21d_buying_pressure_scores_higher():
    """Net buying pressure (close near high) scores higher than selling pressure (close near low)."""
    dates = pd.bdate_range("2020-01-01", periods=40)
    rows = []
    for d in dates:
        vol = 1_000_000.0
        rows.append({"date": d, "ticker": "BUY",  "open": 100.0, "high": 102.0,
                     "low": 98.0, "close": 101.5, "volume": vol})
        rows.append({"date": d, "ticker": "SELL", "open": 100.0, "high": 102.0,
                     "low": 98.0, "close": 98.5,  "volume": vol})
    ohlcv = pd.DataFrame(rows)
    scores = _latest_scores(compute_chaikin_money_flow_21d_scores(ohlcv), "chaikin_money_flow_21d_score")
    assert scores["BUY"] > scores["SELL"]


def test_price_volume_trend_21d_uptrend_scores_higher():
    """Rising price with consistent volume generates higher PVT trend than declining price."""
    dates = pd.bdate_range("2020-01-01", periods=120)
    price_rows, vol_rows = [], []
    for d, i in zip(dates, range(120)):
        price_rows.append({"date": d, "ticker": "UP",   "close": 100.0 + i * 0.3})
        price_rows.append({"date": d, "ticker": "DOWN", "close": 100.0 - i * 0.2})
        vol_rows.append({"date": d, "ticker": "UP",   "volume": 1_000_000.0})
        vol_rows.append({"date": d, "ticker": "DOWN", "volume": 1_000_000.0})
    prices  = pd.DataFrame(price_rows)
    volumes = pd.DataFrame(vol_rows)
    scores = _latest_scores(
        compute_price_volume_trend_21d_scores(prices, volumes), "price_volume_trend_21d_score"
    )
    assert scores["UP"] > scores["DOWN"]

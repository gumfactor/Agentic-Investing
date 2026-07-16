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


# ─── BUG-010 missing-data acceptance tests ───────────────────────────────────
#
# These cover indicators whose gap-tolerance is not automatic from raising
# min_periods alone (mask-multiply / cumsum-based), per the 01B-1 inventory.

def _prices_with_gap(n_days: int = 100, gap_index: int = 50) -> tuple[pd.DataFrame, "pd.Timestamp"]:
    """GAPPY is missing one session; KEEPALIVE trades every session so the gap
    date still exists as a row in the pivoted wide matrix (a NaN cell for
    GAPPY, not a vanished date) — matching how a real multi-ticker universe
    keeps a trading date live even when a single ticker halts for a day.
    """
    import numpy as np

    dates = pd.bdate_range("2020-01-01", periods=n_days)
    rows = []
    # Both tickers oscillate (both up and down days), with a slight positive
    # drift for GAPPY, so up/down-volume ratios are always finite for both —
    # keeping the cross-sectional z-score well-defined even on dates where
    # GAPPY's own value is suppressed by the gap.
    keepalive = 50.0 + 5.0 * np.sin(np.arange(n_days) * 0.7)
    gappy = 100.0 + 0.05 * np.arange(n_days) + 6.0 * np.sin(np.arange(n_days) * 0.9)
    for i, d in enumerate(dates):
        rows.append({"date": d, "ticker": "KEEPALIVE", "close": float(keepalive[i])})
        if i == gap_index:
            continue
        rows.append({"date": d, "ticker": "GAPPY", "close": float(gappy[i])})
    return pd.DataFrame(rows), dates[gap_index]


def _volumes_for(prices: pd.DataFrame) -> pd.DataFrame:
    rows = [
        {"date": d, "ticker": t, "volume": 1_000_000.0}
        for d, t in prices[["date", "ticker"]].itertuples(index=False)
    ]
    return pd.DataFrame(rows)


def test_volume_up_down_ratio_21d_gap_suppresses_value():
    """A gap inside the trailing 21-day window suppresses the ratio entirely,
    instead of silently computing from whichever days had a classifiable
    return (BUG-010: a missing return must never be treated as a non-event
    that a rolling sum quietly absorbs as zero)."""
    prices, gap_date = _prices_with_gap()
    volumes = _volumes_for(prices)
    result = compute_volume_up_down_ratio_21d_scores(prices, volumes)
    scored = result[result["ticker"] == "GAPPY"]
    scored_dates = set(scored["date"])
    all_dates = pd.bdate_range("2020-01-01", periods=100)
    gap_pos = list(all_dates).index(gap_date)
    # Every date whose trailing 21-day window touches the gap date must be
    # absent from the (NaN-dropping) long-format output for GAPPY.
    window_dates = all_dates[gap_pos : gap_pos + 21]
    for d in window_dates:
        assert d not in scored_dates, f"{d} should be suppressed by the gap at {gap_date}"
    # And GAPPY recovers once a full 21-day gap-free window is available again.
    assert all_dates[-1] in scored_dates


def test_obv_momentum_21d_gap_suppresses_value():
    """A gap inside the trailing lookback window suppresses OBV momentum,
    even though OBV itself is a cumulative sum that would otherwise stay
    numeric across the gap (cumsum treats NaN as a zero contribution)."""
    prices, gap_date = _prices_with_gap()
    volumes = _volumes_for(prices)
    result = compute_obv_momentum_21d_scores(prices, volumes)
    scored = result[result["ticker"] == "GAPPY"]
    scored_dates = set(scored["date"])
    all_dates = pd.bdate_range("2020-01-01", periods=100)
    gap_pos = list(all_dates).index(gap_date)
    window_dates = all_dates[gap_pos : gap_pos + 21]
    for d in window_dates:
        assert d not in scored_dates, f"{d} should be suppressed by the gap at {gap_date}"
    assert all_dates[-1] in scored_dates


# ─── BUG-010 adversarial-review fix round: cumsum/EWM/zero-fill twins ────────

def _ohlcv_with_gap(n_days: int = 120, gap_index: int = 60) -> tuple[pd.DataFrame, "pd.Timestamp"]:
    """OHLCV where GAPPY is missing one full bar; KEEPALIVE trades every
    session so the gap date still exists in the pivoted wide matrices."""
    import numpy as np
    rng = np.random.default_rng(23)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    rows = []
    price = 100.0
    for i, d in enumerate(dates):
        for ticker, base in (("KEEPALIVE", 50.0 + 2.0 * np.sin(i * 0.6)), ("GAPPY", None)):
            if ticker == "GAPPY":
                if i == gap_index:
                    continue
                price = max(price * (1 + rng.normal(0.0005, 0.015)), 1.0)
                close = price
            else:
                close = base
            spread = max(abs(rng.normal(0, 0.01)) * close, 0.01)
            open_ = close * (1 + rng.normal(0, 0.004))
            high = max(open_, close) + spread
            low = max(min(open_, close) - spread, 0.01)
            rows.append({
                "date": d, "ticker": ticker,
                "open": float(open_), "high": float(high),
                "low": float(low), "close": float(close),
                "volume": float(abs(rng.normal(1_000_000, 150_000)) + 10_000),
            })
    return pd.DataFrame(rows), dates[gap_index]


def _assert_gap_window_suppressed(result, score_col, gap_date, window, n_days=120):
    scored_dates = set(result[result["ticker"] == "GAPPY"]["date"])
    all_dates = pd.bdate_range("2020-01-01", periods=n_days)
    gap_pos = list(all_dates).index(gap_date)
    for d in all_dates[gap_pos : gap_pos + window]:
        assert d not in scored_dates, f"{d} should be suppressed by the gap at {gap_date}"
    assert all_dates[-1] in scored_dates, "indicator must recover after the gap ages out"


def test_ad_line_momentum_21d_gap_suppresses_value():
    """The A/D line is a cumsum (NaN flow contributes 0, series stays
    numeric), so without the gate the 21-day delta recovers bit-identical to
    the gap-free series one day after a gap — the confirmed BUG-010 twin of
    the OBV/PVT defect."""
    ohlcv, gap_date = _ohlcv_with_gap()
    result = compute_ad_line_momentum_21d_scores(ohlcv)
    _assert_gap_window_suppressed(result, "ad_line_momentum_21d_score", gap_date, 21)


def test_chaikin_oscillator_gap_suppresses_value():
    """Chaikin layers EWMs over the cumsum A/D line; both mechanisms hide a
    gap. The gate uses the slower EMA span (10) as its window."""
    ohlcv, gap_date = _ohlcv_with_gap()
    result = compute_chaikin_oscillator_scores(ohlcv)
    _assert_gap_window_suppressed(result, "chaikin_oscillator_score", gap_date, 10)


def test_money_flow_index_14d_gap_suppresses_value():
    """NaN tp_change compares False to both > 0 and < 0, so .where(..., 0.0)
    fabricates a zero flow that the rolling sums quietly absorb. The gate
    suppresses every window whose trailing 14 tp_change values span the gap
    (the gap knocks out the gap day and the following day's diff)."""
    ohlcv, gap_date = _ohlcv_with_gap()
    result = compute_money_flow_index_14d_scores(ohlcv)
    _assert_gap_window_suppressed(result, "money_flow_index_14d_score", gap_date, 15)


def test_chaikin_money_flow_21d_gap_suppresses_value():
    """CMF's rolling sums see the NaN flow/volume cells directly, so the
    full-window min_periods (21) suppresses any window containing the gap."""
    ohlcv, gap_date = _ohlcv_with_gap()
    result = compute_chaikin_money_flow_21d_scores(ohlcv)
    _assert_gap_window_suppressed(result, "chaikin_money_flow_21d_score", gap_date, 21)


def test_ease_of_movement_14d_gap_suppresses_frozen_value():
    """EOM feeds an EWM; without the gate the EWM decays through the missing
    midpoint_change and emits a frozen duplicate on/after the gap."""
    ohlcv, gap_date = _ohlcv_with_gap()
    result = compute_ease_of_movement_14d_scores(ohlcv)
    # midpoint_change is NaN on the gap day and the day after → 15 sessions
    # whose trailing 14 inputs span the gap.
    _assert_gap_window_suppressed(result, "ease_of_movement_14d_score", gap_date, 15)


def test_force_index_13d_gap_suppresses_frozen_value():
    """Force Index feeds an EWM of (return × volume); the gate suppresses
    sessions whose trailing 13 returns span the gap."""
    prices, gap_date = _prices_with_gap()
    volumes = _volumes_for(prices)
    result = compute_force_index_13d_scores(prices, volumes)
    scored_dates = set(result[result["ticker"] == "GAPPY"]["date"])
    all_dates = pd.bdate_range("2020-01-01", periods=100)
    gap_pos = list(all_dates).index(gap_date)
    for d in all_dates[gap_pos : gap_pos + 14]:
        assert d not in scored_dates, f"{d} should be suppressed by the gap at {gap_date}"
    assert all_dates[-1] in scored_dates

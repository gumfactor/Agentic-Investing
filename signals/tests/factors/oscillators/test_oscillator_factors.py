"""Unit tests for signals/factors/oscillators/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.factors.oscillators.bollinger.bb_pct_b_20 import compute_bb_pct_b_20_scores
from signals.factors.oscillators.bollinger.bb_width_20 import compute_bb_width_20_scores
from signals.factors.oscillators.bollinger.bb_z_score_20 import compute_bb_z_score_20_scores
from signals.factors.oscillators.macd.macd_histogram_12_26_9 import compute_macd_histogram_12_26_9_scores
from signals.factors.oscillators.macd.macd_signal_line_12_26_9 import compute_macd_signal_line_12_26_9_scores
from signals.factors.oscillators.macd.ppo_12_26 import compute_ppo_12_26_scores
from signals.factors.oscillators.macd.dpo_20 import compute_dpo_20_scores
from signals.factors.oscillators.mean_reversion.rolling_zscore_63d import compute_rolling_zscore_63d_scores
from signals.factors.oscillators.mean_reversion.rolling_zscore_252d import compute_rolling_zscore_252d_scores
from signals.factors.oscillators.mean_reversion.price_vs_vwap_21d import compute_price_vs_vwap_21d_scores
from signals.factors.oscillators.momentum.rsi_14 import compute_rsi_14_scores
from signals.factors.oscillators.momentum.rsi_28 import compute_rsi_28_scores
from signals.factors.oscillators.momentum.roc_10 import compute_roc_10_scores
from signals.factors.oscillators.momentum.roc_21 import compute_roc_21_scores
from signals.factors.oscillators.momentum.cci_20 import compute_cci_20_scores
from signals.factors.oscillators.momentum.stoch_k_14 import compute_stoch_k_14_scores
from signals.factors.oscillators.momentum.stoch_d_14 import compute_stoch_d_14_scores
from signals.factors.oscillators.momentum.stoch_rsi_14 import compute_stoch_rsi_14_scores
from signals.factors.oscillators.momentum.williams_r_14 import compute_williams_r_14_scores

from signals.tests.factors.conftest import make_prices, make_volumes, make_ohlc, _latest_scores

# ─── Smoke tests ─────────────────────────────────────────────────────────────

_PRICES_CASES = [
    (compute_bb_pct_b_20_scores,            "bb_pct_b_20_score"),
    (compute_bb_width_20_scores,            "bb_width_20_score"),
    (compute_bb_z_score_20_scores,          "bb_z_score_20_score"),
    (compute_macd_histogram_12_26_9_scores, "macd_histogram_12_26_9_score"),
    (compute_macd_signal_line_12_26_9_scores, "macd_signal_line_12_26_9_score"),
    (compute_ppo_12_26_scores,              "ppo_12_26_score"),
    (compute_dpo_20_scores,                 "dpo_20_score"),
    (compute_rolling_zscore_63d_scores,     "rolling_zscore_63d_score"),
    (compute_rsi_14_scores,                 "rsi_14_score"),
    (compute_rsi_28_scores,                 "rsi_28_score"),
    (compute_roc_10_scores,                 "roc_10_score"),
    (compute_roc_21_scores,                 "roc_21_score"),
    (compute_stoch_k_14_scores,             "stoch_k_14_score"),
    (compute_stoch_d_14_scores,             "stoch_d_14_score"),
    (compute_stoch_rsi_14_scores,           "stoch_rsi_14_score"),
    (compute_williams_r_14_scores,          "williams_r_14_score"),
]


@pytest.mark.parametrize("fn,score_col", _PRICES_CASES, ids=[c[1] for c in _PRICES_CASES])
def test_oscillator_price_only_smoke(fn, score_col, prices_300d):
    result = fn(prices_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


def test_rolling_zscore_252d_smoke(prices_400d):
    result = compute_rolling_zscore_252d_scores(prices_400d)
    assert {"date", "ticker", "rolling_zscore_252d_score"} <= set(result.columns)
    assert len(result) > 0


def test_cci_20_smoke(prices_300d):
    # Implementation uses close-only (standard deviation-of-mean variant), not typical price
    result = compute_cci_20_scores(prices_300d)
    assert {"date", "ticker", "cci_20_score"} <= set(result.columns)
    assert len(result) > 0


def test_price_vs_vwap_21d_smoke(prices_300d, volumes_300d):
    result = compute_price_vs_vwap_21d_scores(prices_300d, volumes_300d)
    assert {"date", "ticker", "price_vs_vwap_21d_score"} <= set(result.columns)
    assert len(result) > 0


# ─── Behavioral tests ────────────────────────────────────────────────────────

def test_rsi_14_rising_stock_scores_higher():
    """A stock with a clear uptrend should have a higher RSI than one with a clear downtrend."""
    import numpy as np
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    up, down, flat = 100.0, 100.0, 100.0
    for d in dates:
        up   = max(up   + rng.normal(+0.5, 0.5), 1.0)
        down = max(down + rng.normal(-0.5, 0.5), 1.0)
        flat = max(flat + rng.normal(0.0,  0.5), 1.0)
        rows.append({"date": d, "ticker": "UP",   "close": up})
        rows.append({"date": d, "ticker": "DOWN", "close": down})
        rows.append({"date": d, "ticker": "FLAT", "close": flat})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_rsi_14_scores(prices), "rsi_14_score")
    assert scores["UP"] > scores["DOWN"]


def test_rolling_zscore_63d_above_mean_scores_higher():
    """A stock trading above its 63d mean should score higher than one at its mean."""
    import numpy as np
    rng = np.random.default_rng(5)
    dates = pd.bdate_range("2020-01-01", periods=200)
    rows = []
    for d, i in zip(dates, range(200)):
        # HIGH jumps sharply above its recent mean in the last 20 days
        high_close = 100.0 + rng.normal(0, 0.5) + (10.0 if i >= 170 else 0.0)
        low_close  = 100.0 + rng.normal(0, 0.5)
        rows.append({"date": d, "ticker": "HIGH", "close": float(high_close)})
        rows.append({"date": d, "ticker": "LOW",  "close": float(low_close)})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_rolling_zscore_63d_scores(prices), "rolling_zscore_63d_score")
    assert scores["HIGH"] > scores["LOW"]


def test_price_vs_vwap_21d_above_vwap_scores_higher():
    """A stock that very recently jumped above its VWAP scores higher (VWAP lags the jump)."""
    import numpy as np
    rng = np.random.default_rng(9)
    # Use 100 days; price jumps in the last 5 days so VWAP hasn't caught up
    dates = pd.bdate_range("2020-01-01", periods=100)
    price_rows, vol_rows = [], []
    for d, i in zip(dates, range(100)):
        above = 100.0 + rng.normal(0, 0.2) + (8.0 if i >= 95 else 0.0)
        below = 100.0 + rng.normal(0, 0.2) - (8.0 if i >= 95 else 0.0)
        flat  = 100.0 + rng.normal(0, 0.2)
        for ticker, close in [("ABOVE", above), ("BELOW", below), ("MID", flat)]:
            price_rows.append({"date": d, "ticker": ticker, "close": float(close)})
            vol_rows.append({"date": d, "ticker": ticker, "volume": 1_000_000.0})
    prices  = pd.DataFrame(price_rows)
    volumes = pd.DataFrame(vol_rows)
    scores  = _latest_scores(compute_price_vs_vwap_21d_scores(prices, volumes), "price_vs_vwap_21d_score")
    assert scores["ABOVE"] > scores["BELOW"]


def test_price_vs_vwap_21d_empty_volumes_raises(prices_300d):
    with pytest.raises(ValueError):
        compute_price_vs_vwap_21d_scores(prices_300d, pd.DataFrame())


def test_oscillator_empty_prices_raises():
    with pytest.raises(ValueError):
        compute_rsi_14_scores(pd.DataFrame(columns=["date", "ticker", "close"]))


def test_bb_pct_b_above_upper_band_scores_higher():
    """Price recently pushed above BB upper band should score higher than one at the lower band."""
    import numpy as np
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    for d, i in zip(dates, range(100)):
        above = 100.0 + rng.normal(0, 0.5) + (8.0 if i >= 85 else 0.0)
        below = 100.0 + rng.normal(0, 0.5) - (8.0 if i >= 85 else 0.0)
        rows.append({"date": d, "ticker": "ABOVE", "close": float(above)})
        rows.append({"date": d, "ticker": "BELOW", "close": float(below)})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_bb_pct_b_20_scores(prices), "bb_pct_b_20_score")
    assert scores["ABOVE"] > scores["BELOW"]


def test_macd_histogram_acceleration_scores_higher():
    """Price that recently started trending up generates positive MACD histogram vs decelerating price."""
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    for d, i in zip(dates, range(100)):
        # ACCEL: flat for 85 days then rises sharply — fast EMA reacts before slow EMA
        accel = 100.0 if i < 85 else 100.0 + (i - 85) * 1.5
        # DECEL: rises for 85 days then flattens — both EMAs converge toward flat value
        decel = 100.0 + i * 0.5 if i < 85 else 100.0 + 85 * 0.5
        rows.append({"date": d, "ticker": "ACCEL", "close": float(accel)})
        rows.append({"date": d, "ticker": "DECEL", "close": float(decel)})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_macd_histogram_12_26_9_scores(prices), "macd_histogram_12_26_9_score")
    assert scores["ACCEL"] > scores["DECEL"]


def test_roc_10_rising_stock_scores_higher():
    """A stock with a positive 10-day return should score higher than a declining one."""
    dates = pd.bdate_range("2020-01-01", periods=60)
    rows = []
    for d, i in zip(dates, range(60)):
        rows.append({"date": d, "ticker": "UP",   "close": 100.0 + i * 0.3})
        rows.append({"date": d, "ticker": "DOWN", "close": 100.0 - i * 0.3})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_roc_10_scores(prices), "roc_10_score")
    assert scores["UP"] > scores["DOWN"]

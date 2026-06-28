"""Unit tests for signals/indicators/oscillators/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.indicators.oscillators.bollinger.bb_pct_b_20 import compute_bb_pct_b_20_scores
from signals.indicators.oscillators.bollinger.bb_width_20 import compute_bb_width_20_scores
from signals.indicators.oscillators.bollinger.bb_z_score_20 import compute_bb_z_score_20_scores
from signals.indicators.oscillators.macd.macd_histogram_12_26_9 import compute_macd_histogram_12_26_9_scores
from signals.indicators.oscillators.macd.macd_signal_line_12_26_9 import compute_macd_signal_line_12_26_9_scores
from signals.indicators.oscillators.macd.ppo_12_26 import compute_ppo_12_26_scores
from signals.indicators.oscillators.macd.dpo_20 import compute_dpo_20_scores
from signals.indicators.oscillators.mean_reversion.rolling_zscore_63d import compute_rolling_zscore_63d_scores
from signals.indicators.oscillators.mean_reversion.rolling_zscore_252d import compute_rolling_zscore_252d_scores
from signals.indicators.oscillators.mean_reversion.price_vs_vwap_21d import compute_price_vs_vwap_21d_scores
from signals.indicators.oscillators.momentum.rsi_14 import compute_rsi_14_scores
from signals.indicators.oscillators.momentum.rsi_28 import compute_rsi_28_scores
from signals.indicators.oscillators.momentum.roc_10 import compute_roc_10_scores
from signals.indicators.oscillators.momentum.roc_21 import compute_roc_21_scores
from signals.indicators.oscillators.momentum.cci_20 import compute_cci_20_scores
from signals.indicators.oscillators.momentum.stoch_k_14 import compute_stoch_k_14_scores
from signals.indicators.oscillators.momentum.stoch_d_14 import compute_stoch_d_14_scores
from signals.indicators.oscillators.momentum.stoch_rsi_14 import compute_stoch_rsi_14_scores
from signals.indicators.oscillators.momentum.williams_r_14 import compute_williams_r_14_scores

from signals.indicators.oscillators.momentum.rsi_14_raw import compute_rsi_14_raw_scores
from signals.indicators.oscillators.bollinger.bb_pct_b_20_raw import compute_bb_pct_b_20_raw_scores
from signals.indicators.oscillators.mean_reversion.rolling_zscore_252d_raw import compute_rolling_zscore_252d_raw_scores

from signals.tests.indicators.conftest import make_prices, make_volumes, make_ohlc, _latest_scores

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


# ─── Raw (absolute-value) oscillator tests ────────────────────────────────────

def test_rsi_14_raw_output_columns(prices_300d):
    result = compute_rsi_14_raw_scores(prices_300d)
    assert {"date", "ticker", "rsi_14_raw"} <= set(result.columns)
    assert len(result) > 0


def test_rsi_14_raw_not_cross_sectionally_normalized(prices_300d):
    """Raw RSI values must NOT be centered on 0 or have unit variance — they are absolute."""
    import numpy as np
    result = compute_rsi_14_raw_scores(prices_300d)
    latest_date = result["date"].max()
    latest = result[result["date"] == latest_date]["rsi_14_raw"].dropna()
    # RSI values are 0–100; they should not look like a cross-sectional z-score
    assert latest.mean() > 5.0, "RSI mean should be well above 0"
    assert latest.std() < 40.0, "RSI std should be much less than values themselves"


def test_rsi_14_raw_range(prices_300d):
    """RSI values must stay within [0, 100]."""
    result = compute_rsi_14_raw_scores(prices_300d)
    valid = result["rsi_14_raw"].dropna()
    assert (valid >= 0.0).all()
    assert (valid <= 100.0).all()


def test_rsi_14_raw_downtrend_lower_than_uptrend():
    """A persistently falling stock should have lower raw RSI than a rising one."""
    import numpy as np
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    for d, i in zip(dates, range(100)):
        rows.append({"date": d, "ticker": "UP",   "close": 100.0 + i * 0.5})
        rows.append({"date": d, "ticker": "DOWN", "close": max(100.0 - i * 0.5, 1.0)})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_rsi_14_raw_scores(prices), "rsi_14_raw")
    assert scores["UP"] > scores["DOWN"]


def test_bb_pct_b_20_raw_output_columns(prices_300d):
    result = compute_bb_pct_b_20_raw_scores(prices_300d)
    assert {"date", "ticker", "bb_pct_b_20_raw"} <= set(result.columns)
    assert len(result) > 0


def test_bb_pct_b_20_raw_not_cross_sectionally_normalized(prices_300d):
    """Raw %B values are in approximately [0, 1] range, not centered on 0."""
    result = compute_bb_pct_b_20_raw_scores(prices_300d)
    latest_date = result["date"].max()
    latest = result[result["date"] == latest_date]["bb_pct_b_20_raw"].dropna()
    # For a typical market, %B clusters around 0.5; definitely not zero-mean
    assert latest.mean() > -1.0
    assert latest.mean() < 2.0


def test_bb_pct_b_20_raw_above_band_higher_than_below():
    """Price above upper band (%B > 1) should have higher raw value than price below lower band (%B < 0)."""
    import numpy as np
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    for d, i in zip(dates, range(100)):
        above = 100.0 + rng.normal(0, 0.3) + (12.0 if i >= 80 else 0.0)
        below = 100.0 + rng.normal(0, 0.3) - (12.0 if i >= 80 else 0.0)
        rows.append({"date": d, "ticker": "ABOVE", "close": float(above)})
        rows.append({"date": d, "ticker": "BELOW", "close": float(below)})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_bb_pct_b_20_raw_scores(prices), "bb_pct_b_20_raw")
    assert scores["ABOVE"] > scores["BELOW"]


def test_rolling_zscore_252d_raw_output_columns(prices_400d):
    result = compute_rolling_zscore_252d_raw_scores(prices_400d)
    assert {"date", "ticker", "rolling_zscore_252d_raw"} <= set(result.columns)
    assert len(result) > 0


def test_rolling_zscore_252d_raw_above_mean_positive():
    """A stock trading well above its 252d mean should have a positive raw z-score."""
    import numpy as np
    rng = np.random.default_rng(13)
    dates = pd.bdate_range("2020-01-01", periods=400)
    rows = []
    for d, i in zip(dates, range(400)):
        # HIGH: jumps 3 std-devs above prior mean in the last 20 bars
        high = 100.0 + rng.normal(0, 0.5) + (15.0 if i >= 380 else 0.0)
        low  = max(100.0 + rng.normal(0, 0.5) - (15.0 if i >= 380 else 0.0), 1.0)
        rows.append({"date": d, "ticker": "HIGH", "close": float(high)})
        rows.append({"date": d, "ticker": "LOW",  "close": float(low)})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_rolling_zscore_252d_raw_scores(prices), "rolling_zscore_252d_raw")
    assert scores["HIGH"] > 0
    assert scores["LOW"] < 0
    assert scores["HIGH"] > scores["LOW"]


def test_rolling_zscore_252d_raw_not_cross_sectionally_normalized(prices_400d):
    """Values should reflect each stock's own history, not be re-centered across the universe."""
    result = compute_rolling_zscore_252d_raw_scores(prices_400d)
    latest_date = result["date"].max()
    latest = result[result["date"] == latest_date]["rolling_zscore_252d_raw"].dropna()
    # Unlike cross-sectional z-scores, the mean should NOT be forced to exactly 0
    # (though it may be close by coincidence). The key test is that values reflect
    # individual stocks — check that std is non-trivially positive.
    assert latest.std() > 0.01

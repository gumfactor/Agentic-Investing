"""Unit tests for signals/indicators/volatility/* factors."""
from __future__ import annotations

import pandas as pd
import pytest

from signals.indicators.volatility.realized.realized_vol_10d import compute_realized_vol_10d_scores
from signals.indicators.volatility.realized.realized_vol_21d import compute_realized_vol_21d_scores
from signals.indicators.volatility.realized.realized_vol_63d import compute_realized_vol_63d_scores
from signals.indicators.volatility.realized.realized_vol_252d import compute_realized_vol_252d_scores
from signals.indicators.volatility.realized.vol_of_vol_21d import compute_vol_of_vol_21d_scores
from signals.indicators.volatility.regime.vol_percentile_252d import compute_vol_percentile_252d_scores
from signals.indicators.volatility.regime.vol_ratio_21d_63d import compute_vol_ratio_21d_63d_scores
from signals.indicators.volatility.regime.vol_ratio_21d_252d import compute_vol_ratio_21d_252d_scores
from signals.indicators.volatility.regime.vol_trend_slope_63d import compute_vol_trend_slope_63d_scores
from signals.indicators.volatility.adjusted_return.sharpe_ratio_63d import compute_sharpe_ratio_63d_scores
from signals.indicators.volatility.adjusted_return.sharpe_ratio_252d import compute_sharpe_ratio_252d_scores
from signals.indicators.volatility.adjusted_return.sortino_ratio_63d import compute_sortino_ratio_63d_scores
from signals.indicators.volatility.adjusted_return.calmar_ratio_63d import compute_calmar_ratio_63d_scores
from signals.indicators.volatility.adjusted_return.vol_adjusted_mom_12m import compute_vol_adjusted_mom_12m_scores
from signals.indicators.volatility.downside.max_drawdown_63d import compute_max_drawdown_63d_scores
from signals.indicators.volatility.downside.downside_deviation_63d import compute_downside_deviation_63d_scores
from signals.indicators.volatility.downside.upside_deviation_63d import compute_upside_deviation_63d_scores
from signals.indicators.volatility.downside.up_down_vol_ratio_63d import compute_up_down_vol_ratio_63d_scores
from signals.indicators.volatility.downside.ulcer_index_63d import compute_ulcer_index_63d_scores
from signals.indicators.volatility.ohlc.atr_14 import compute_atr_14_scores
from signals.indicators.volatility.ohlc.atr_ratio_14_63 import compute_atr_ratio_14_63_scores
from signals.indicators.volatility.ohlc.garman_klass_vol_21d import compute_garman_klass_vol_21d_scores
from signals.indicators.volatility.ohlc.intraday_range_21d import compute_intraday_range_21d_scores
from signals.indicators.volatility.ohlc.parkinson_vol_21d import compute_parkinson_vol_21d_scores
from signals.indicators.volatility.systematic.beta_63d import compute_beta_63d_scores
from signals.indicators.volatility.systematic.beta_252d import compute_beta_252d_scores
from signals.indicators.volatility.systematic.beta_stability_63d import compute_beta_stability_63d_scores
from signals.indicators.volatility.systematic.idiosyncratic_vol_63d import compute_idiosyncratic_vol_63d_scores

from signals.tests.indicators.conftest import make_prices, make_ohlc, _latest_scores

# ─── Smoke tests ─────────────────────────────────────────────────────────────

_PRICE_SHORT_CASES = [
    (compute_realized_vol_10d_scores,      "realized_vol_10d_score"),
    (compute_realized_vol_21d_scores,      "realized_vol_21d_score"),
    (compute_realized_vol_63d_scores,      "realized_vol_63d_score"),
    (compute_vol_of_vol_21d_scores,        "vol_of_vol_21d_score"),
    (compute_vol_ratio_21d_63d_scores,     "vol_ratio_21d_63d_score"),
    (compute_vol_trend_slope_63d_scores,   "vol_trend_slope_63d_score"),
    (compute_sharpe_ratio_63d_scores,      "sharpe_ratio_63d_score"),
    (compute_sortino_ratio_63d_scores,     "sortino_ratio_63d_score"),
    (compute_calmar_ratio_63d_scores,      "calmar_ratio_63d_score"),
    (compute_max_drawdown_63d_scores,      "max_drawdown_63d_score"),
    (compute_downside_deviation_63d_scores, "downside_deviation_63d_score"),
    (compute_upside_deviation_63d_scores,  "upside_deviation_63d_score"),
    (compute_up_down_vol_ratio_63d_scores, "up_down_vol_ratio_63d_score"),
    (compute_ulcer_index_63d_scores,       "ulcer_index_63d_score"),
]

_PRICE_LONG_CASES = [
    (compute_realized_vol_252d_scores,     "realized_vol_252d_score"),
    (compute_vol_percentile_252d_scores,   "vol_percentile_252d_score"),
    (compute_vol_ratio_21d_252d_scores,    "vol_ratio_21d_252d_score"),
    (compute_sharpe_ratio_252d_scores,     "sharpe_ratio_252d_score"),
    (compute_vol_adjusted_mom_12m_scores,  "vol_adjusted_mom_12m_score"),
]

_OHLC_CASES = [
    (compute_atr_14_scores,             "atr_14_score"),
    (compute_atr_ratio_14_63_scores,    "atr_ratio_14_63_score"),
    (compute_garman_klass_vol_21d_scores, "garman_klass_vol_21d_score"),
    (compute_intraday_range_21d_scores, "intraday_range_21d_score"),
    (compute_parkinson_vol_21d_scores,  "parkinson_vol_21d_score"),
]


@pytest.mark.parametrize("fn,score_col", _PRICE_SHORT_CASES, ids=[c[1] for c in _PRICE_SHORT_CASES])
def test_volatility_price_short_smoke(fn, score_col, prices_300d):
    result = fn(prices_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,score_col", _PRICE_LONG_CASES, ids=[c[1] for c in _PRICE_LONG_CASES])
def test_volatility_price_long_smoke(fn, score_col, prices_400d):
    result = fn(prices_400d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


@pytest.mark.parametrize("fn,score_col", _OHLC_CASES, ids=[c[1] for c in _OHLC_CASES])
def test_volatility_ohlc_smoke(fn, score_col, ohlc_300d):
    result = fn(ohlc_300d)
    assert {"date", "ticker", score_col} <= set(result.columns)
    assert len(result) > 0


def test_beta_63d_smoke(prices_with_spy_400d):
    result = compute_beta_63d_scores(prices_with_spy_400d)
    assert {"date", "ticker", "beta_63d_score"} <= set(result.columns)
    assert "SPY" not in result["ticker"].values
    assert len(result) > 0


def test_beta_252d_smoke(prices_with_spy_400d):
    result = compute_beta_252d_scores(prices_with_spy_400d)
    assert {"date", "ticker", "beta_252d_score"} <= set(result.columns)
    assert "SPY" not in result["ticker"].values
    assert len(result) > 0


def test_beta_stability_63d_smoke(prices_with_spy_400d):
    result = compute_beta_stability_63d_scores(prices_with_spy_400d)
    assert {"date", "ticker", "beta_stability_63d_score"} <= set(result.columns)
    assert "SPY" not in result["ticker"].values
    assert len(result) > 0


def test_idiosyncratic_vol_63d_smoke(prices_with_spy_400d):
    result = compute_idiosyncratic_vol_63d_scores(prices_with_spy_400d)
    assert {"date", "ticker", "idiosyncratic_vol_63d_score"} <= set(result.columns)
    assert "SPY" not in result["ticker"].values
    assert len(result) > 0


# ─── Behavioral tests ────────────────────────────────────────────────────────

def test_realized_vol_21d_high_vol_scores_higher():
    """A high-volatility stock should produce a higher raw vol score."""
    dates = pd.bdate_range("2020-01-01", periods=100)
    rows = []
    import numpy as np
    rng = np.random.default_rng(0)
    for d in dates:
        rows.append({"date": d, "ticker": "HIGHVOL", "close": float(100 + rng.normal(0, 5))})
        rows.append({"date": d, "ticker": "LOWVOL",  "close": float(100 + rng.normal(0, 0.1))})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_realized_vol_21d_scores(prices), "realized_vol_21d_score")
    assert scores["HIGHVOL"] > scores["LOWVOL"]


def test_max_drawdown_63d_large_drawdown_scores_higher():
    """A stock that experienced a large drawdown within the 63d window scores higher.
    Use with negative strategy weight to prefer low-drawdown stocks."""
    import numpy as np
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2020-01-01", periods=150)
    rows = []
    for d, i in zip(dates, range(150)):
        crash  = 100.0 * (0.6 if 100 <= i <= 120 else 1.0) + rng.normal(0, 0.1)
        stable = 100.0 + rng.normal(0, 0.1)
        noisy  = 100.0 + rng.normal(0, 2.0)
        rows.append({"date": d, "ticker": "CRASH",  "close": max(float(crash),  0.01)})
        rows.append({"date": d, "ticker": "STABLE", "close": max(float(stable), 0.01)})
        rows.append({"date": d, "ticker": "NOISY",  "close": max(float(noisy),  0.01)})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_max_drawdown_63d_scores(prices), "max_drawdown_63d_score")
    assert scores["CRASH"] > scores["STABLE"]


def test_sharpe_ratio_63d_better_risk_adj_return_scores_higher():
    """Consistent positive return with low vol should outscore high-vol mean-zero."""
    import numpy as np
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2020-01-01", periods=150)
    rows = []
    p_good, p_bad, p_mid = 100.0, 100.0, 100.0
    for d in dates:
        p_good = max(p_good * (1.0 + rng.normal(+0.002, 0.005)), 1.0)
        p_bad  = max(p_bad  * (1.0 + rng.normal( 0.000, 0.05)),  1.0)
        p_mid  = max(p_mid  * (1.0 + rng.normal(+0.001, 0.02)),  1.0)
        rows.append({"date": d, "ticker": "GOOD", "close": p_good})
        rows.append({"date": d, "ticker": "BAD",  "close": p_bad})
        rows.append({"date": d, "ticker": "MID",  "close": p_mid})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_sharpe_ratio_63d_scores(prices), "sharpe_ratio_63d_score")
    assert scores["GOOD"] > scores["BAD"]


def test_beta_missing_spy_raises():
    with pytest.raises(ValueError, match="SPY"):
        compute_beta_252d_scores(make_prices())


def test_volatility_empty_prices_raises():
    with pytest.raises(ValueError):
        compute_realized_vol_21d_scores(pd.DataFrame(columns=["date", "ticker", "close"]))


def test_ohlc_missing_columns_raises():
    with pytest.raises(ValueError):
        compute_atr_14_scores(pd.DataFrame(columns=["date", "ticker", "close"]))


def test_vol_ratio_21d_63d_expanding_vol_scores_higher():
    """Stock with recent vol spike relative to its longer-term baseline scores higher."""
    import numpy as np
    rng = np.random.default_rng(13)
    dates = pd.bdate_range("2020-01-01", periods=120)
    rows = []
    p_expand = 100.0
    p_stable = 100.0
    for d, i in zip(dates, range(120)):
        if i < 90:
            p_expand = max(p_expand + rng.normal(0, 0.1), 1.0)
        else:
            p_expand = max(p_expand + rng.normal(0, 2.5), 1.0)
        p_stable = max(p_stable + rng.normal(0, 0.5), 1.0)
        rows.append({"date": d, "ticker": "EXPAND", "close": p_expand})
        rows.append({"date": d, "ticker": "STABLE", "close": p_stable})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_vol_ratio_21d_63d_scores(prices), "vol_ratio_21d_63d_score")
    assert scores["EXPAND"] > scores["STABLE"]


def test_atr_14_high_range_scores_higher():
    """Stock with wider daily H-L range scores higher on ATR."""
    dates = pd.bdate_range("2020-01-01", periods=40)
    rows = []
    for d in dates:
        rows.append({"date": d, "ticker": "WIDE",
                     "open": 98.0, "high": 105.0, "low": 95.0, "close": 100.0})
        rows.append({"date": d, "ticker": "TIGHT",
                     "open": 99.9, "high": 100.2, "low": 99.8, "close": 100.0})
    ohlc = pd.DataFrame(rows)
    scores = _latest_scores(compute_atr_14_scores(ohlc), "atr_14_score")
    assert scores["WIDE"] > scores["TIGHT"]


def test_beta_63d_high_beta_scores_higher():
    """Stock that amplifies SPY moves scores higher than a defensive low-beta stock."""
    import numpy as np
    rng = np.random.default_rng(17)
    dates = pd.bdate_range("2020-01-01", periods=150)
    rows = []
    spy_p, hi_p, lo_p = 300.0, 100.0, 100.0
    for d in dates:
        spy_ret = rng.normal(0, 0.01)
        spy_p = max(spy_p * (1 + spy_ret), 1.0)
        hi_p  = max(hi_p  * (1 + 2.5 * spy_ret + rng.normal(0, 0.003)), 1.0)
        lo_p  = max(lo_p  * (1 + 0.2 * spy_ret + rng.normal(0, 0.003)), 1.0)
        rows.append({"date": d, "ticker": "SPY",      "close": spy_p})
        rows.append({"date": d, "ticker": "HIGHBETA", "close": hi_p})
        rows.append({"date": d, "ticker": "LOWBETA",  "close": lo_p})
    prices = pd.DataFrame(rows)
    scores = _latest_scores(compute_beta_63d_scores(prices), "beta_63d_score")
    assert scores["HIGHBETA"] > scores["LOWBETA"]


# ─── BUG-010 missing-data acceptance tests ───────────────────────────────────

def _prices_with_gap(n_days: int = 150, gap_index: int = 90) -> tuple[pd.DataFrame, pd.Timestamp]:
    """GAPPY is missing one session; KEEPALIVE trades every session so the gap
    date still exists as a row in the pivoted wide matrix (a NaN cell for
    GAPPY's price, not a vanished date)."""
    import numpy as np
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    rows = []
    price = 100.0
    for i, d in enumerate(dates):
        price = max(price * (1 + rng.normal(0.0003, 0.01)), 1.0)
        rows.append({"date": d, "ticker": "KEEPALIVE", "close": 80.0 + i * 0.05})
        if i == gap_index:
            continue
        rows.append({"date": d, "ticker": "GAPPY", "close": price})
    return pd.DataFrame(rows), dates[gap_index]


def test_realized_vol_21d_gap_suppresses_value_not_zero():
    """A gap inside the trailing 21-day return window suppresses the vol
    score for every date whose window touches the gap, rather than computing
    a (biased) value from a non-contiguous subset of returns (BUG-010)."""
    prices, gap_date = _prices_with_gap()
    result = compute_realized_vol_21d_scores(prices)
    scored_dates = set(result[result["ticker"] == "GAPPY"]["date"])
    all_dates = pd.bdate_range("2020-01-01", periods=150)
    gap_pos = list(all_dates).index(gap_date)
    window_dates = all_dates[gap_pos : gap_pos + 21]
    for d in window_dates:
        assert d not in scored_dates, f"{d} should be suppressed by the gap at {gap_date}"
    # Recovers once a full 21-day gap-free window is available again.
    assert all_dates[-1] in scored_dates


def test_sharpe_ratio_63d_gap_suppresses_value():
    """Same acceptance criterion applied to a two-sided (mean and std) ratio
    statistic: a gap anywhere in the trailing 63-day window suppresses the
    Sharpe ratio for that date. n_days is generous enough that the fixture
    still has >= 63 gap-free sessions after the gap for recovery to be
    possible within the window."""
    n_days, gap_index = 220, 90
    prices, gap_date = _prices_with_gap(n_days=n_days, gap_index=gap_index)
    result = compute_sharpe_ratio_63d_scores(prices)
    scored_dates = set(result[result["ticker"] == "GAPPY"]["date"])
    all_dates = pd.bdate_range("2020-01-01", periods=n_days)
    gap_pos = list(all_dates).index(gap_date)
    window_dates = all_dates[gap_pos : gap_pos + 63]
    for d in window_dates:
        assert d not in scored_dates, f"{d} should be suppressed by the gap at {gap_date}"
    assert all_dates[-1] in scored_dates


def test_vol_trend_slope_63d_gap_tolerance():
    """Documented exception (see docs/plans/01b1-pct-change-inventory.md):
    the outer 63-point OLS trend fit tolerates gaps in the underlying vol_21
    series down to its own internal mask.sum() >= 20 threshold, rather than
    requiring the full 63-point window. A single missing return (which only
    knocks out a handful of vol_21 values around the gap, well above 20
    remaining valid points in any 63-window that reaches this far past
    warm-up) must NOT suppress the slope the way the default full-window
    policy would for other indicators."""
    n_days, gap_index = 220, 100
    prices, gap_date = _prices_with_gap(n_days=n_days, gap_index=gap_index)
    result = compute_vol_trend_slope_63d_scores(prices)
    scored_dates = set(result[result["ticker"] == "GAPPY"]["date"])
    all_dates = pd.bdate_range("2020-01-01", periods=n_days)
    # Well after the gap has aged out of both the 21d and 63d windows, the
    # slope must be defined again (proving the gap doesn't propagate forever
    # the way require_full_window's default suppression would).
    assert all_dates[-1] in scored_dates

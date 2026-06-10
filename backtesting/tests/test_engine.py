"""Tests for BacktestEngine and supporting functions."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from backtesting.engine.data_handler import DataHandler
from backtesting.engine.event_loop import (
    BacktestEngine,
    _compute_metrics,
    _hash_config,
    _max_drawdown,
    _select_equal_weight,
)
from backtesting.engine.fill_simulator import FillSimulator


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

def _make_prices(n_days: int = 60, tickers: list[str] | None = None) -> pd.DataFrame:
    """Generate synthetic price data starting 2023-01-02."""
    if tickers is None:
        tickers = ["AAPL", "GOOG", "MSFT", "AMZN", "META"]
    start = date(2023, 1, 2)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    # Remove weekends
    dates = [d for d in dates if d.weekday() < 5]
    rows = []
    for d in dates:
        for t in tickers:
            rows.append({"ticker": t, "date": d, "close": 100.0 + hash(t) % 50})
    return pd.DataFrame(rows)


def _make_signals(n_days: int = 60, tickers: list[str] | None = None) -> pd.DataFrame:
    """Generate synthetic alpha scores."""
    if tickers is None:
        tickers = ["AAPL", "GOOG", "MSFT", "AMZN", "META"]
    start = date(2023, 1, 2)
    dates = [start + timedelta(days=i) for i in range(0, n_days, 21)]  # monthly scores
    dates = [d for d in dates if d.weekday() < 5]
    rows = []
    for i, d in enumerate(dates):
        for j, t in enumerate(tickers):
            rows.append({"ticker": t, "score_date": d, "alpha_score": float(j + i * 0.1)})
    return pd.DataFrame(rows)


def _make_benchmark(n_days: int = 60) -> pd.DataFrame:
    """Generate synthetic benchmark (SPY) prices."""
    start = date(2023, 1, 2)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    dates = [d for d in dates if d.weekday() < 5]
    prices = [400.0 * (1 + 0.001 * i) for i in range(len(dates))]
    return pd.DataFrame({"date": dates, "close": prices})


def _make_config(start: str = "2023-01-02", end: str = "2023-03-31") -> dict:
    return {
        "name": "test_strategy",
        "version": 1,
        "data_version": "snapshot-v1",
        "portfolio": {
            "method": "equal_weight",
            "n_long": 3,
            "rebalance_frequency": "monthly",
        },
        "backtest": {
            "start_date": start,
            "end_date": end,
            "initial_capital": 100_000.0,
            "benchmark": "SPY",
        },
        "execution": {
            "fill_model": "perfect",
        },
    }


# ------------------------------------------------------------------
# _select_equal_weight
# ------------------------------------------------------------------

def test_select_equal_weight_top_n():
    signals = pd.DataFrame({
        "ticker": ["A", "B", "C", "D", "E"],
        "score_date": [date(2023, 1, 2)] * 5,
        "alpha_score": [5.0, 3.0, 1.0, 4.0, 2.0],
    })
    weights = _select_equal_weight(signals, n_long=3)
    assert set(weights.keys()) == {"A", "D", "B"}  # top 3
    assert all(abs(w - 1 / 3) < 1e-9 for w in weights.values())


def test_select_equal_weight_fewer_than_n():
    signals = pd.DataFrame({
        "ticker": ["A", "B"],
        "score_date": [date(2023, 1, 2)] * 2,
        "alpha_score": [5.0, 3.0],
    })
    weights = _select_equal_weight(signals, n_long=10)
    assert set(weights.keys()) == {"A", "B"}
    assert all(abs(w - 0.5) < 1e-9 for w in weights.values())


def test_select_equal_weight_empty():
    signals = pd.DataFrame(columns=["ticker", "score_date", "alpha_score"])
    weights = _select_equal_weight(signals, n_long=5)
    assert weights == {}


# ------------------------------------------------------------------
# _max_drawdown
# ------------------------------------------------------------------

def test_max_drawdown_flat():
    returns = pd.Series([0.01, 0.01, 0.01])
    assert _max_drawdown(returns) == pytest.approx(0.0, abs=1e-9)


def test_max_drawdown_decline():
    # 10% drop then partial recovery
    returns = pd.Series([0.0, -0.10, 0.05])
    dd = _max_drawdown(returns)
    assert dd < 0
    assert abs(dd) <= 0.10 + 1e-6


# ------------------------------------------------------------------
# _compute_metrics
# ------------------------------------------------------------------

def test_compute_metrics_keys():
    rng = np.random.default_rng(42)
    rets = pd.Series(rng.normal(0.0005, 0.01, 252))
    bm = pd.Series(rng.normal(0.0004, 0.01, 252))
    metrics = _compute_metrics(rets, bm, pd.DataFrame(), 100_000.0)
    for key in ("sharpe", "cagr", "max_drawdown", "information_ratio", "total_return"):
        assert key in metrics


def test_compute_metrics_empty_returns():
    metrics = _compute_metrics(pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), 100_000.0)
    assert metrics == {}


# ------------------------------------------------------------------
# _hash_config
# ------------------------------------------------------------------

def test_hash_config_deterministic():
    cfg = {"a": 1, "b": {"c": 2}}
    assert _hash_config(cfg) == _hash_config(cfg)


def test_hash_config_sensitive_to_values():
    cfg1 = {"a": 1}
    cfg2 = {"a": 2}
    assert _hash_config(cfg1) != _hash_config(cfg2)


# ------------------------------------------------------------------
# DataHandler
# ------------------------------------------------------------------

def test_data_handler_pit_enforcement():
    prices = _make_prices(30, ["AAPL"])
    signals = pd.DataFrame({
        "ticker": ["AAPL"],
        "score_date": [date(2023, 2, 1)],
        "alpha_score": [1.0],
    })
    benchmark = _make_benchmark(30)
    handler = DataHandler(prices, signals, benchmark)

    # Signal date is 2023-02-01; querying on 2023-01-15 should return empty
    early = handler.get_latest_signals(date(2023, 1, 15))
    assert early.empty

    # Querying on 2023-02-01 should return the signal
    on_date = handler.get_latest_signals(date(2023, 2, 1))
    assert len(on_date) == 1


# ------------------------------------------------------------------
# BacktestEngine end-to-end
# ------------------------------------------------------------------

def test_engine_runs_end_to_end():
    prices = _make_prices(90, ["AAPL", "GOOG", "MSFT", "AMZN", "META"])
    signals = _make_signals(90, ["AAPL", "GOOG", "MSFT", "AMZN", "META"])
    benchmark = _make_benchmark(90)
    handler = DataHandler(prices, signals, benchmark)

    config = _make_config("2023-01-02", "2023-03-31")
    fill_sim = FillSimulator(fill_model="perfect")
    engine = BacktestEngine()

    result = engine.run(config, handler, fill_sim)

    assert not result.nav_series.empty
    assert not result.returns.empty
    assert "sharpe" in result.metrics
    assert result.data_version == "snapshot-v1"
    assert len(result.config_hash) == 64  # SHA-256 hex


def test_engine_reproducible():
    prices = _make_prices(90, ["AAPL", "GOOG", "MSFT"])
    signals = _make_signals(90, ["AAPL", "GOOG", "MSFT"])
    benchmark = _make_benchmark(90)
    handler = DataHandler(prices, signals, benchmark)
    config = _make_config("2023-01-02", "2023-03-31")
    fill_sim = FillSimulator(fill_model="perfect")
    engine = BacktestEngine()

    r1 = engine.run(config, handler, fill_sim)
    r2 = engine.run(config, handler, fill_sim)

    pd.testing.assert_series_equal(r1.nav_series, r2.nav_series)
    pd.testing.assert_series_equal(r1.returns, r2.returns)


def test_engine_no_rebalance_without_signals():
    prices = _make_prices(30, ["AAPL"])
    # No signals at all
    signals = pd.DataFrame(columns=["ticker", "score_date", "alpha_score"])
    benchmark = _make_benchmark(30)
    handler = DataHandler(prices, signals, benchmark)
    config = _make_config("2023-01-02", "2023-01-31")
    fill_sim = FillSimulator(fill_model="perfect")
    engine = BacktestEngine()

    result = engine.run(config, handler, fill_sim)
    assert result.trades.empty
    # All cash: NAV should be constant at initial capital
    assert all(abs(v - 100_000.0) < 1e-6 for v in result.nav_series)


def test_engine_no_dates_raises():
    prices = _make_prices(5, ["AAPL"])
    signals = _make_signals(5, ["AAPL"])
    benchmark = _make_benchmark(5)
    handler = DataHandler(prices, signals, benchmark)
    # Date range outside available data
    config = _make_config("2025-01-01", "2025-12-31")
    fill_sim = FillSimulator(fill_model="perfect")
    engine = BacktestEngine()
    with pytest.raises(ValueError, match="No trading dates"):
        engine.run(config, handler, fill_sim)

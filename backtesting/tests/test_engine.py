"""Tests for BacktestEngine and supporting functions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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
    """Generate synthetic alpha scores.

    Signals start 5 calendar days before the price series (2022-12-28) so
    they are available on the first trading day after the 1-day execution lag.
    """
    if tickers is None:
        tickers = ["AAPL", "GOOG", "MSFT", "AMZN", "META"]
    signal_start = date(2022, 12, 28)  # 5 days before 2023-01-02 price start
    dates = [signal_start + timedelta(days=i) for i in range(0, n_days, 21)]
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

    # Earlier date: no signal yet
    early = handler.get_latest_signals(date(2023, 1, 15))
    assert early.empty

    # Same day as signal: NOT tradeable (signal uses that day's close — look-ahead)
    same_day = handler.get_latest_signals(date(2023, 2, 1))
    assert same_day.empty

    # Next day: tradeable
    next_day = handler.get_latest_signals(date(2023, 2, 2))
    assert len(next_day) == 1


def test_data_handler_no_same_day_execution():
    """Confirm that score_date == sim_date is explicitly blocked."""
    prices = _make_prices(5, ["AAPL"])
    signals = pd.DataFrame({
        "ticker": ["AAPL", "AAPL"],
        "score_date": [date(2023, 1, 3), date(2023, 1, 4)],
        "alpha_score": [1.0, 2.0],
    })
    benchmark = _make_benchmark(5)
    handler = DataHandler(prices, signals, benchmark)

    # On 2023-01-04: only the 2023-01-03 score is visible, not the 2023-01-04 one
    visible = handler.get_latest_signals(date(2023, 1, 4))
    assert len(visible) == 1
    assert visible.iloc[0]["score_date"] == date(2023, 1, 3)


def test_engine_cash_never_negative():
    """Full initial deployment with transaction costs must not push cash negative."""
    tickers = ["AAPL", "GOOG", "MSFT"]
    prices = _make_prices(60, tickers)
    # Signals available before the first price date so trading starts immediately
    signals = pd.DataFrame([
        {"ticker": t, "score_date": date(2022, 12, 28), "alpha_score": float(i)}
        for i, t in enumerate(tickers)
    ])
    benchmark = _make_benchmark(60)
    handler = DataHandler(prices, signals, benchmark)

    config = _make_config("2023-01-02", "2023-03-15")
    fill_sim = FillSimulator(
        bid_ask_spread_bps=20.0,
        market_impact_coeff=0.5,
        commission_per_share=0.01,
        fill_model="transaction_cost",
    )
    result = BacktestEngine().run(config, handler, fill_sim)

    assert (result.nav_series >= 0).all(), "NAV went negative — cash constraint violated"


def test_compute_orders_deterministic():
    """Order list must be identical regardless of dict insertion order."""
    from backtesting.engine.fill_simulator import compute_orders
    tickers = [f"T{i:03d}" for i in range(50)]
    target = {t: 0.02 for t in tickers}
    current = {t: 0.015 for t in tickers}
    # Reverse insertion order — a set-based implementation would differ
    target_rev = dict(reversed(list(target.items())))
    current_rev = dict(reversed(list(current.items())))
    orders1 = compute_orders(target, current)
    orders2 = compute_orders(target_rev, current_rev)
    assert [o.ticker for o in orders1] == [o.ticker for o in orders2]


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


# ------------------------------------------------------------------
# _select_equal_weight — max_position_weight
# ------------------------------------------------------------------

def test_select_equal_weight_cap_applies():
    """Weight = min(1/N, max_position_weight); residual stays in cash."""
    signals = pd.DataFrame({
        "ticker": ["A", "B", "C"],
        "score_date": [date(2023, 1, 2)] * 3,
        "alpha_score": [3.0, 2.0, 1.0],
    })
    # 1/3 ≈ 0.333 > 0.25 → cap at 0.25 per position
    weights = _select_equal_weight(signals, n_long=3, max_position_weight=0.25)
    assert set(weights.keys()) == {"A", "B", "C"}
    assert all(abs(w - 0.25) < 1e-9 for w in weights.values())
    assert abs(sum(weights.values()) - 0.75) < 1e-9  # 0.25 cash in reserve


def test_select_equal_weight_cap_no_effect_below_threshold():
    """Cap has no effect when 1/N <= max_position_weight."""
    signals = pd.DataFrame({
        "ticker": ["A", "B", "C", "D", "E"],
        "score_date": [date(2023, 1, 2)] * 5,
        "alpha_score": [5.0, 4.0, 3.0, 2.0, 1.0],
    })
    # 1/5 = 0.20 <= 0.25 → unchanged
    weights = _select_equal_weight(signals, n_long=5, max_position_weight=0.25)
    assert all(abs(w - 0.20) < 1e-9 for w in weights.values())


# ------------------------------------------------------------------
# min_holding_days enforcement
# ------------------------------------------------------------------

def _make_daily_config(start: str = "2023-01-02", end: str = "2023-03-31") -> dict:
    cfg = _make_config(start, end)
    cfg["portfolio"]["rebalance_frequency"] = "daily"
    cfg["portfolio"]["min_holding_days"] = 5   # 5 trading days lock
    return cfg


def test_min_holding_days_prevents_early_sell():
    """A position opened today must NOT be sold within min_holding_days.

    Setup: n_long=1, daily rebalance, min_holding_days=5.
    Day-0 signal: AAPL score=10 → AAPL bought on 2023-01-02.
    Day-1 signal: GOOG score=10 → engine wants to sell AAPL and buy GOOG.
    Lock must prevent that SELL for at least 5 trading days.
    """
    tickers = ["AAPL", "GOOG", "MSFT"]
    prices = _make_prices(60, tickers)
    signal_day0 = date(2022, 12, 28)
    signal_day1 = date(2023, 1, 3)   # one trading day after 2023-01-02
    signals = pd.DataFrame([
        {"ticker": "AAPL", "score_date": signal_day0, "alpha_score": 10.0},
        {"ticker": "GOOG", "score_date": signal_day0, "alpha_score": 1.0},
        {"ticker": "GOOG", "score_date": signal_day1, "alpha_score": 10.0},
        {"ticker": "AAPL", "score_date": signal_day1, "alpha_score": 1.0},
    ])
    benchmark = _make_benchmark(60)
    handler = DataHandler(prices, signals, benchmark)

    config = _make_daily_config("2023-01-02", "2023-01-20")
    config["portfolio"]["n_long"] = 1

    fill_sim = FillSimulator(fill_model="perfect")
    result = BacktestEngine().run(config, handler, fill_sim)

    trades = result.trades
    assert not trades.empty, "Expected trades; engine produced none"

    aapl_buys = trades[(trades["ticker"] == "AAPL") & (trades["direction"] == "BUY")]
    assert not aapl_buys.empty, "AAPL must be bought on the first rebalance"

    aapl_sells = trades[(trades["ticker"] == "AAPL") & (trades["direction"] == "SELL")]
    assert not aapl_sells.empty, "AAPL must eventually be sold (signal flipped to GOOG)"

    # AAPL bought on 2023-01-02 (trading day 0).
    # min_holding_days=5 → first allowed sell is trading day 5.
    # Trading days: Jan2=0, Jan3=1, Jan4=2, Jan5=3, Jan6=4, Jan9=5 → allow sell from Jan9.
    first_sell = aapl_sells["date"].min()
    assert first_sell >= date(2023, 1, 9), (
        f"AAPL sold on {first_sell} but min_holding_days=5 requires >= 2023-01-09"
    )


def test_engine_max_position_weight_via_config():
    """max_position_weight from config is respected: per-position weight capped."""
    tickers = ["AAPL", "GOOG", "MSFT"]
    prices = _make_prices(60, tickers)
    signals = pd.DataFrame([
        {"ticker": t, "score_date": date(2022, 12, 28), "alpha_score": float(i)}
        for i, t in enumerate(tickers)
    ])
    benchmark = _make_benchmark(60)
    handler = DataHandler(prices, signals, benchmark)

    config = _make_config("2023-01-02", "2023-03-15")
    config["portfolio"]["n_long"] = 3
    config["portfolio"]["max_position_weight"] = 0.25   # 1/3 > 0.25 → cap binds

    fill_sim = FillSimulator(fill_model="perfect")
    result = BacktestEngine().run(config, handler, fill_sim)

    # Positions DataFrame should show no ticker exceeding 0.25 + small tolerance
    if not result.positions.empty:
        max_weight = result.positions.max().max()
        assert max_weight <= 0.25 + 1e-4, (
            f"max_position_weight=0.25 violated; observed {max_weight:.4f}"
        )


# ------------------------------------------------------------------
# backtesting/loader.py — Finding #3
# ------------------------------------------------------------------

def _make_loader_prices(tickers=("AAPL",)) -> pd.DataFrame:
    rows = []
    for i in range(30):
        d = date(2023, 1, 2) + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        for t in tickers:
            rows.append({"ticker": t, "date": d, "close": 150.0, "source": "yfinance"})
    return pd.DataFrame(rows)


def test_loader_adjust_prices_no_actions():
    """With no corporate actions all adj_factors = 1.0; close unchanged."""
    from backtesting.loader import _adjust_prices
    prices = _make_loader_prices()
    corp = pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
    result = _adjust_prices(prices, corp)
    assert "close" in result.columns
    assert all(abs(result["close"] - 150.0) < 1e-6)


def test_loader_adjust_prices_split():
    """A 2-for-1 split: pre-split unadjusted=300, post-split unadjusted=150.
    Adjustment should bring pre-split adj_close down to 150 (300 * 0.5).
    Post-split prices remain at 150 (adj_factor=1).
    """
    from backtesting.loader import _adjust_prices
    split_date = date(2023, 1, 10)

    # Use different prices before and after the split so the boundary is testable.
    rows = []
    for i in range(20):
        d = date(2023, 1, 2) + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        close = 300.0 if d < split_date else 150.0   # realistic: price halves on ex-date
        rows.append({"ticker": "AAPL", "date": d, "close": close, "source": "yfinance"})
    prices = pd.DataFrame(rows)

    corp = pd.DataFrame([{
        "ticker": "AAPL",
        "ex_date": split_date,
        "action_type": "split",
        "value": 2.0,
    }])
    result = _adjust_prices(prices, corp)
    before_split = result[result["date"] < split_date]
    after_split = result[result["date"] >= split_date]
    # Pre-split unadjusted=300, factor=0.5 → adj_close=150
    assert all(abs(before_split["close"] - 150.0) < 1e-3), before_split["close"].tolist()
    # Post-split unadjusted=150, factor=1.0 → adj_close=150
    assert all(abs(after_split["close"] - 150.0) < 1e-3), after_split["close"].tolist()


# ------------------------------------------------------------------
# backtesting/dataset_manifest.py — Finding #1
# ------------------------------------------------------------------

def test_build_manifest_row_counts():
    """build_manifest captures correct row counts for each data type."""
    from backtesting.dataset_manifest import build_manifest
    prices = _make_loader_prices(["AAPL", "GOOG"])
    alpha = pd.DataFrame({
        "ticker": ["AAPL"], "score_date": [date(2023, 1, 2)],
        "strategy_id": ["v1"], "alpha_score": [1.0],
    })
    corp = pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
    bm = pd.DataFrame({"date": [date(2023, 1, 2)], "close": [400.0]})

    manifest = build_manifest(
        version="2026-06-10",
        strategy_id="v1",
        dataframes={
            "daily_prices": prices,
            "alpha_scores": alpha,
            "corporate_actions": corp,
            "benchmark": bm,
        },
        object_paths={
            "daily_prices": "rqis-snapshots/snapshots/daily_prices/2026-06-10/data.parquet",
        },
        snapshot_dates={"daily_prices": date(2026, 6, 10)},
    )

    assert manifest.version == "2026-06-10"
    assert manifest.strategy_id == "v1"
    assert manifest.row_counts["daily_prices"] == len(prices)
    assert manifest.row_counts["alpha_scores"] == 1
    assert manifest.row_counts["corporate_actions"] == 0
    assert len(manifest.git_commit) > 0
    assert "daily_prices" in manifest.date_ranges


def test_build_manifest_schema_hashes_differ_by_columns():
    """Schema hashes differ when column sets differ."""
    from backtesting.dataset_manifest import build_manifest
    df1 = pd.DataFrame({"ticker": [], "date": [], "close": []})
    df2 = pd.DataFrame({"ticker": [], "score_date": [], "alpha_score": []})
    m1 = build_manifest("v1", "v1", {"a": df1}, {}, {})
    m2 = build_manifest("v1", "v1", {"a": df2}, {}, {})
    assert m1.schema_hashes["a"] != m2.schema_hashes["a"]


# ------------------------------------------------------------------
# Finding #8 — alpha_scores_sha256 content hash
# ------------------------------------------------------------------

def test_manifest_alpha_hash_changes_on_score_mutation():
    """alpha_scores_sha256 must change if any score value changes."""
    from backtesting.dataset_manifest import build_manifest
    alpha_a = pd.DataFrame({
        "ticker": ["AAPL", "GOOG"],
        "score_date": [date(2023, 1, 2)] * 2,
        "strategy_id": ["v1"] * 2,
        "alpha_score": [1.5, 2.5],
    })
    alpha_b = alpha_a.copy()
    alpha_b.loc[0, "alpha_score"] = 1.6  # mutate one score

    m_a = build_manifest("v1", "v1", {"alpha_scores": alpha_a}, {}, {})
    m_b = build_manifest("v1", "v1", {"alpha_scores": alpha_b}, {}, {})
    assert m_a.alpha_scores_sha256 != m_b.alpha_scores_sha256


def test_manifest_alpha_hash_stable_under_row_reorder():
    """alpha_scores_sha256 must be identical regardless of DataFrame row order."""
    from backtesting.dataset_manifest import build_manifest
    alpha = pd.DataFrame({
        "ticker": ["AAPL", "GOOG"],
        "score_date": [date(2023, 1, 2)] * 2,
        "strategy_id": ["v1"] * 2,
        "alpha_score": [1.5, 2.5],
    })
    alpha_reversed = alpha.iloc[::-1].reset_index(drop=True)

    m1 = build_manifest("v1", "v1", {"alpha_scores": alpha}, {}, {})
    m2 = build_manifest("v1", "v1", {"alpha_scores": alpha_reversed}, {}, {})
    assert m1.alpha_scores_sha256 == m2.alpha_scores_sha256


def test_manifest_alpha_hash_empty():
    """Empty alpha_scores produces a non-empty sentinel hash (not empty string)."""
    from backtesting.dataset_manifest import build_manifest
    empty = pd.DataFrame(columns=["ticker", "score_date", "alpha_score"])
    m = build_manifest("v1", "v1", {"alpha_scores": empty}, {}, {})
    assert len(m.alpha_scores_sha256) == 64  # full SHA-256 hex digest


# ------------------------------------------------------------------
# Finding #10 — strategy_id guard in loader
# ------------------------------------------------------------------

def _mock_snapshots(prices, alpha, benchmark):
    """Build a MagicMock ParquetSnapshots that serves pre-loaded DataFrames.

    corporate_actions raises FileNotFoundError (optional snapshot) so the
    loader falls back to an empty frame, matching the production default.
    """
    from unittest.mock import MagicMock

    data = {"daily_prices": prices, "alpha_scores": alpha, "benchmark": benchmark}

    def _load(data_type, _snap_date):
        if data_type == "corporate_actions":
            raise FileNotFoundError("no corp actions snapshot in test")
        return data[data_type]

    mock = MagicMock()
    mock.load_snapshot.side_effect = _load
    return mock


def test_loader_loads_dotenv_before_constructing_default_snapshots(monkeypatch):
    """The documented local command must work without manually exporting .env."""
    from backtesting.loader import load_from_snapshot
    from data.storage import parquet_snapshots

    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)

    def _load_dotenv():
        monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
        return True

    class _ExpectedStop(Exception):
        pass

    def _construct_snapshots():
        assert os.environ["MINIO_ENDPOINT"] == "localhost:9000"
        raise _ExpectedStop

    monkeypatch.setattr("dotenv.load_dotenv", _load_dotenv)
    monkeypatch.setattr(parquet_snapshots, "ParquetSnapshots", _construct_snapshots)

    with pytest.raises(_ExpectedStop):
        load_from_snapshot("2023-01-02", {"strategy_id": "v1"})


def test_loader_raises_if_strategy_id_column_absent():
    """load_from_snapshot must raise ValueError when strategy_id column is missing."""
    from backtesting.loader import load_from_snapshot

    prices = _make_loader_prices(["AAPL"])
    alpha_no_sid = pd.DataFrame({
        "ticker": ["AAPL"],
        "score_date": [date(2023, 1, 2)],
        "alpha_score": [1.0],  # no strategy_id column
    })
    benchmark = pd.DataFrame({"date": [date(2023, 1, 2)], "close": [400.0]})

    with pytest.raises(ValueError, match="strategy_id"):
        load_from_snapshot(
            "2023-01-02", {"name": "v1"},
            snapshots=_mock_snapshots(prices, alpha_no_sid, benchmark),
        )


def test_loader_warns_when_no_scores_for_strategy():
    """load_from_snapshot returns a DataHandler with empty signals when strategy filter yields nothing."""
    from backtesting.loader import load_from_snapshot

    prices = _make_loader_prices(["AAPL"])
    alpha_wrong_sid = pd.DataFrame({
        "ticker": ["AAPL"],
        "score_date": [date(2023, 1, 2)],
        "strategy_id": ["other_strategy"],
        "alpha_score": [1.0],
    })
    benchmark = pd.DataFrame({"date": [date(2023, 1, 2)], "close": [400.0]})

    handler = load_from_snapshot(
        "2023-01-02", {"name": "v1"},
        snapshots=_mock_snapshots(prices, alpha_wrong_sid, benchmark),
    )
    assert handler.get_latest_signals(date(2023, 1, 3)).empty


def test_loader_prefers_explicit_strategy_id_over_display_name():
    """A human-readable config name must not hide scores stored under strategy_id."""
    from backtesting.loader import load_from_snapshot

    prices = _make_loader_prices(["AAPL"])
    alpha = pd.DataFrame({
        "ticker": ["AAPL"],
        "score_date": [date(2023, 1, 2)],
        "strategy_id": ["v1"],
        "alpha_score": [1.0],
    })
    benchmark = pd.DataFrame({"date": [date(2023, 1, 2)], "close": [400.0]})

    handler = load_from_snapshot(
        "2023-01-02",
        {"name": "base_momentum", "strategy_id": "v1"},
        snapshots=_mock_snapshots(prices, alpha, benchmark),
    )

    signals = handler.get_latest_signals(date(2023, 1, 3))
    assert signals["ticker"].tolist() == ["AAPL"]


# ------------------------------------------------------------------
# scripts/backfill_momentum_scores.py — Finding #4 history guard
# ------------------------------------------------------------------

def _make_backfill_prices(n_trading_days: int, tickers=("AAPL",)) -> pd.DataFrame:
    """Generate a price DataFrame with exactly n_trading_days rows per ticker."""
    from datetime import date as _date, timedelta as _td
    rows = []
    d = _date(2018, 1, 2)
    count = 0
    while count < n_trading_days:
        if d.weekday() < 5:
            for t in tickers:
                rows.append({"ticker": t, "date": d, "close": 100.0, "source": "test"})
            count += 1
        d += _td(days=1)
    return pd.DataFrame(rows)


def test_backfill_raises_on_insufficient_history():
    """backfill run() must raise ValueError when lookback < 273 trading days."""
    from unittest.mock import MagicMock
    from scripts.backfill_momentum_scores import run

    # 100 trading days before start — far below the 273 required.
    start = date(2023, 6, 1)
    prices = _make_backfill_prices(n_trading_days=100)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    mock_snaps = MagicMock()
    mock_snaps.load_snapshot.return_value = prices

    with pytest.raises(ValueError, match="Insufficient price history"):
        run(
            snapshot_date=date(2026, 6, 10),
            start=start,
            end=date(2023, 12, 31),
            strategy_id="v1",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
        )


def test_backfill_passes_with_sufficient_history():
    """backfill run() proceeds past the guard when >= 273 trading days of lookback exist."""
    from unittest.mock import MagicMock
    from scripts.backfill_momentum_scores import run

    # 273 + 50 trading days — enough lookback, plus some dates after start for scores.
    prices_all = _make_backfill_prices(n_trading_days=273 + 50)
    prices_all["date"] = pd.to_datetime(prices_all["date"]).dt.date
    # start is the (273+1)-th trading day so exactly 273 precede it.
    start = sorted(prices_all["date"].unique())[273]

    mock_snaps = MagicMock()
    mock_snaps.load_snapshot.return_value = prices_all

    # dry_run=True exits before DB writes; should not raise.
    # provisional_no_universe=True: this test exercises the lookback guard
    # only — PIT membership enforcement (BUG-008 / 01B-2, on by default) is
    # deliberately bypassed because no universe import DB exists here; it is
    # covered by data/tests/universe/test_acceptance_1_4.py.
    run(
        snapshot_date=date(2026, 6, 10),
        start=start,
        end=prices_all["date"].max(),
        strategy_id="v1",
        batch_size=20,
        dry_run=True,
        snapshots=mock_snaps,
        provisional_no_universe=True,
    )


# ------------------------------------------------------------------
# Cross-process reproducibility (PRD exit criterion)
# ------------------------------------------------------------------

@pytest.mark.slow
def test_engine_reproducible_cross_process():
    """NAV series, returns, and trade sequence must be bit-for-bit identical
    across three independent subprocesses with different PYTHONHASHSEED values.

    Python's hash randomisation (PEP 456) shuffles dict/set iteration order
    across processes.  The sorted() fix in compute_orders() must hold across
    process boundaries — this test proves it does.

    The worker runs as a module so the repo's package layout resolves cleanly:
        PYTHONHASHSEED=<n> python -m backtesting.tests._backtest_subprocess_worker <out>
    """
    _SEEDS = (0, 1, 42)
    _WORKER = "backtesting.tests._backtest_subprocess_worker"

    with tempfile.TemporaryDirectory() as tmpdir:
        outputs = []
        for seed in _SEEDS:
            out_path = os.path.join(tmpdir, f"result_{seed}.json")
            env = {**os.environ, "PYTHONHASHSEED": str(seed)}
            proc = subprocess.run(
                [sys.executable, "-m", _WORKER, out_path],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert proc.returncode == 0, (
                f"Worker subprocess (PYTHONHASHSEED={seed}) exited with "
                f"code {proc.returncode}:\n{proc.stderr}"
            )
            with open(out_path) as fh:
                outputs.append(json.load(fh))

        ref = outputs[0]
        for result, seed in zip(outputs[1:], _SEEDS[1:]):
            assert result["nav_series"] == ref["nav_series"], (
                f"NAV series differs between PYTHONHASHSEED={_SEEDS[0]} "
                f"and PYTHONHASHSEED={seed}"
            )
            assert result["returns"] == ref["returns"], (
                f"Returns series differs at PYTHONHASHSEED={seed}"
            )
            assert result["config_hash"] == ref["config_hash"], (
                f"Config hash differs at PYTHONHASHSEED={seed}"
            )
            assert result["trade_tickers"] == ref["trade_tickers"], (
                f"Trade ticker sequence differs at PYTHONHASHSEED={seed}:\n"
                f"  seed=0:   {ref['trade_tickers']}\n"
                f"  seed={seed}: {result['trade_tickers']}"
            )
            assert result["trade_directions"] == ref["trade_directions"], (
                f"Trade direction sequence differs at PYTHONHASHSEED={seed}"
            )


# ------------------------------------------------------------------
# PIT safety audit script tests
# ------------------------------------------------------------------

def _make_audit_prices(n_days: int = 700, tickers: list[str] | None = None) -> pd.DataFrame:
    """Prices with enough history for momentum (needs 252+21 trading days)."""
    if tickers is None:
        tickers = ["AAPL", "GOOG", "MSFT"]
    start = date(2021, 1, 4)
    dates = []
    d = start
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    rng = np.random.default_rng(99)
    rows = []
    for t in tickers:
        base = {"AAPL": 150.0, "GOOG": 2800.0, "MSFT": 300.0}.get(t, 100.0)
        prices = base * np.cumprod(1 + rng.normal(0, 0.01, n_days))
        for dt, p in zip(dates, prices):
            rows.append({"ticker": t, "date": dt, "close": float(p)})
    return pd.DataFrame(rows)


def test_audit_structural_pass(tmp_path):
    """Structural check passes because DataHandler uses strict < for score_date."""
    import importlib
    audit = importlib.import_module("scripts.audit_pit_safety")
    violations = audit._structural_audit()
    assert violations == [], f"Unexpected structural violations: {violations}"


def test_audit_timing_contract_structural_pass():
    """BUG-009 section 2.4: the timing-contract structural check passes on
    the actual signals.research.timing / signals.research.ic source."""
    import importlib
    audit = importlib.import_module("scripts.audit_pit_safety")
    violations = audit._structural_audit_timing_contract()
    assert violations == [], f"Unexpected timing-contract violations: {violations}"


def test_audit_entry_exit_alignment_clean():
    """BUG-009 section 2.4: entry/exit alignment audit reports zero
    violations on live price data — every row satisfies score_date <
    entry_date < exit_date and forward_return matches the entry/exit close
    recomputation exactly."""
    import importlib
    audit = importlib.import_module("scripts.audit_pit_safety")

    prices_df = _make_audit_prices(n_days=60, tickers=["AAPL", "GOOG"])
    violations = audit._entry_exit_alignment_audit(prices_df, sample_size=50, seed=42)
    assert violations == [], f"Unexpected alignment violations: {violations}"


def test_audit_empirical_clean(tmp_path):
    """Empirical audit reports zero violations on correctly computed scores."""
    import importlib
    from signals.composites.momentum_score import compute_momentum_scores

    audit = importlib.import_module("scripts.audit_pit_safety")

    prices_df = _make_audit_prices()
    raw_scores = compute_momentum_scores(prices_df)
    scores_df = raw_scores.rename(columns={"date": "score_date", "momentum_score": "z_score"})
    scores_df["factor_name"] = "momentum"
    scores_df["strategy_id"] = "v1"

    prices_file = str(tmp_path / "prices.parquet")
    scores_file = str(tmp_path / "scores.parquet")
    prices_df.to_parquet(prices_file)
    scores_df.to_parquet(scores_file)

    n_checked, n_violations, _ = audit._empirical_audit(prices_df, scores_df, sample_size=50, seed=42)
    assert n_checked > 0, "Audit checked no pairs"
    assert n_violations == 0, f"Expected 0 violations; got {n_violations}"


def test_audit_empirical_accepts_momentum_only_alpha_scores():
    """Pinned v1 alpha scores can be audited when factor scores are absent."""
    import importlib

    from signals.composites.momentum_score import compute_momentum_scores

    audit = importlib.import_module("scripts.audit_pit_safety")
    prices_df = _make_audit_prices()
    scores_df = compute_momentum_scores(prices_df).rename(
        columns={"date": "score_date", "momentum_score": "alpha_score"}
    )
    scores_df["strategy_id"] = "v1"

    n_checked, n_violations, _ = audit._empirical_audit(
        prices_df, scores_df, sample_size=50, seed=42
    )

    assert n_checked == 50
    assert n_violations == 0


def test_audit_snapshot_falls_back_to_v1_alpha_scores(monkeypatch):
    """The standard backtest bundle omits factor_scores but includes alpha_scores."""
    import importlib

    from data.storage import parquet_snapshots

    audit = importlib.import_module("scripts.audit_pit_safety")
    prices_df = _make_audit_prices()
    alpha_df = pd.DataFrame(
        {
            "ticker": ["AAPL"],
            "score_date": [date(2023, 1, 3)],
            "strategy_id": ["v1"],
            "alpha_score": [1.0],
        }
    )

    class _Snapshots:
        def load_snapshot(self, data_type, snapshot_date):
            assert snapshot_date == date(2026, 6, 14)
            if data_type == "daily_prices":
                return prices_df
            if data_type == "factor_scores":
                raise FileNotFoundError
            if data_type == "alpha_scores":
                return alpha_df
            raise AssertionError(data_type)

    monkeypatch.setattr(parquet_snapshots, "ParquetSnapshots", _Snapshots)

    prices, scores = audit._load_from_snapshot(date(2026, 6, 14), "v1")

    assert prices is prices_df
    assert scores.equals(alpha_df)


def test_audit_empirical_detects_corrupted_scores(tmp_path):
    """Empirical audit reports violations when stored scores are wrong."""
    import importlib
    from signals.composites.momentum_score import compute_momentum_scores

    audit = importlib.import_module("scripts.audit_pit_safety")

    prices_df = _make_audit_prices()
    raw_scores = compute_momentum_scores(prices_df)
    scores_df = raw_scores.rename(columns={"date": "score_date", "momentum_score": "z_score"})
    scores_df["factor_name"] = "momentum"
    scores_df["strategy_id"] = "v1"

    # Corrupt 5 scores with nonsense values
    corrupted = scores_df.copy()
    corrupted.iloc[:5, corrupted.columns.get_loc("z_score")] = 999.0

    n_checked, n_violations, violation_records = audit._empirical_audit(
        prices_df, corrupted, sample_size=len(corrupted), seed=0
    )
    assert n_violations >= 5, (
        f"Expected at least 5 violations for 5 corrupted rows; got {n_violations}"
    )

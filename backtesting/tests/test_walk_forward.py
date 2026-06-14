"""Tests for WalkForwardValidator and _build_fold_dates."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from backtesting.validation.walk_forward import _build_fold_dates, WalkForwardValidator
from backtesting.engine.data_handler import DataHandler
from backtesting.engine.fill_simulator import FillSimulator
from backtesting.engine.event_loop import BacktestEngine


def _make_trading_dates(n: int) -> list[date]:
    start = date(2015, 1, 5)  # Monday
    result = []
    d = start
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d)
        d += timedelta(days=1)
    return result


def _make_prices(dates: list[date], tickers: list[str]) -> pd.DataFrame:
    rows = []
    for d in dates:
        for t in tickers:
            rows.append({"ticker": t, "date": d, "close": 100.0})
    return pd.DataFrame(rows)


def _make_signals(dates: list[date], tickers: list[str]) -> pd.DataFrame:
    rows = []
    for i, d in enumerate(dates[::21]):  # monthly
        for j, t in enumerate(tickers):
            rows.append({"ticker": t, "score_date": d, "alpha_score": float(j)})
    return pd.DataFrame(rows)


def _make_benchmark(dates: list[date]) -> pd.DataFrame:
    return pd.DataFrame({"date": dates, "close": [400.0 + i * 0.1 for i in range(len(dates))]})


# ------------------------------------------------------------------
# _build_fold_dates
# ------------------------------------------------------------------

def test_build_fold_dates_correct_count():
    dates = _make_trading_dates(1000)
    folds = _build_fold_dates(dates, n_folds=3, train_years=2.0, test_months=6, window_type="expanding")
    assert len(folds) == 3


def test_build_fold_dates_train_before_test():
    dates = _make_trading_dates(1000)
    folds = _build_fold_dates(dates, n_folds=3, train_years=2.0, test_months=6, window_type="expanding")
    for tr_start, tr_end, te_start, te_end in folds:
        assert tr_end < te_start


def test_build_fold_dates_expanding_same_start():
    dates = _make_trading_dates(1000)
    folds = _build_fold_dates(dates, n_folds=3, train_years=2.0, test_months=6, window_type="expanding")
    starts = [f[0] for f in folds]
    assert all(s == starts[0] for s in starts)


def test_build_fold_dates_rolling_advances():
    dates = _make_trading_dates(1500)
    folds = _build_fold_dates(dates, n_folds=3, train_years=2.0, test_months=6, window_type="rolling")
    starts = [f[0] for f in folds]
    # Rolling: each fold's train_start should advance
    assert starts[0] <= starts[1] <= starts[2]
    # At least one should differ
    assert starts[0] != starts[2]


def test_build_fold_dates_insufficient_data_raises():
    dates = _make_trading_dates(100)  # Way too few
    with pytest.raises(ValueError, match="Insufficient data"):
        _build_fold_dates(dates, n_folds=3, train_years=3.0, test_months=12, window_type="expanding")


def test_build_fold_dates_sequential_test_windows():
    dates = _make_trading_dates(1200)
    folds = _build_fold_dates(dates, n_folds=3, train_years=2.0, test_months=6, window_type="expanding")
    # Each test window starts after the previous test window
    for i in range(1, len(folds)):
        assert folds[i][2] > folds[i - 1][3]


# ------------------------------------------------------------------
# WalkForwardValidator
# ------------------------------------------------------------------

def test_walk_forward_validator_returns_correct_fold_count():
    dates = _make_trading_dates(1200)
    tickers = ["A", "B", "C", "D", "E"]
    prices = _make_prices(dates, tickers)
    signals = _make_signals(dates, tickers)
    benchmark = _make_benchmark(dates)
    handler = DataHandler(prices, signals, benchmark)

    config = {
        "name": "test",
        "version": 1,
        "data_version": "v1",
        "portfolio": {"method": "equal_weight", "n_long": 3, "rebalance_frequency": "monthly"},
        "backtest": {
            "start_date": str(dates[0]),
            "end_date": str(dates[-1]),
            "initial_capital": 100_000.0,
        },
        "execution": {"fill_model": "perfect"},
    }

    validator = WalkForwardValidator(engine=BacktestEngine(), fill_simulator=FillSimulator(fill_model="perfect"))
    result = validator.run(config, handler, n_folds=2, train_years=2.0, test_months=6)

    assert len(result.folds) == 2


def test_walk_forward_oos_returns_chronological():
    dates = _make_trading_dates(1200)
    tickers = ["A", "B", "C"]
    prices = _make_prices(dates, tickers)
    signals = _make_signals(dates, tickers)
    benchmark = _make_benchmark(dates)
    handler = DataHandler(prices, signals, benchmark)

    config = {
        "name": "test",
        "version": 1,
        "data_version": "v1",
        "portfolio": {"method": "equal_weight", "n_long": 2, "rebalance_frequency": "monthly"},
        "backtest": {
            "start_date": str(dates[0]),
            "end_date": str(dates[-1]),
            "initial_capital": 100_000.0,
        },
        "execution": {"fill_model": "perfect"},
    }

    validator = WalkForwardValidator(engine=BacktestEngine(), fill_simulator=FillSimulator(fill_model="perfect"))
    result = validator.run(config, handler, n_folds=2, train_years=2.0, test_months=6)

    # OOS returns should be sorted by date
    idx = result.oos_returns.index.tolist()
    assert idx == sorted(idx)

"""Tests for ParameterSweeper and helpers."""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from backtesting.engine.data_handler import DataHandler
from backtesting.engine.event_loop import BacktestEngine
from backtesting.engine.fill_simulator import FillSimulator
from backtesting.validation.parameter_sensitivity import (
    ParameterSensitivityResult,
    ParameterSensitivityRow,
    ParameterSweeper,
    _apply_params,
    _set_nested,
)


# ------------------------------------------------------------------
# Synthetic data helpers (shared with walk_forward tests)
# ------------------------------------------------------------------

def _make_trading_dates(n: int) -> list[date]:
    start = date(2015, 1, 5)
    result: list[date] = []
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
    for d in dates[::21]:
        for j, t in enumerate(tickers):
            rows.append({"ticker": t, "score_date": d, "alpha_score": float(j)})
    return pd.DataFrame(rows)


def _make_benchmark(dates: list[date]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": dates,
        "close": [400.0 + i * 0.1 for i in range(len(dates))],
    })


def _make_handler(n_dates: int = 900) -> DataHandler:
    tickers = ["A", "B", "C", "D", "E"]
    dates = _make_trading_dates(n_dates)
    return DataHandler(
        _make_prices(dates, tickers),
        _make_signals(dates, tickers),
        _make_benchmark(dates),
    )


def _base_config(dates: list[date] | None = None) -> dict:
    if dates is None:
        dates = _make_trading_dates(900)
    return {
        "name": "test_sweep",
        "version": 1,
        "data_version": "v1",
        "portfolio": {
            "method": "equal_weight",
            "n_long": 3,
            "rebalance_frequency": "monthly",
            "min_holding_days": 0,
            "max_position_weight": 1.0,
        },
        "backtest": {
            "start_date": str(dates[0]),
            "end_date": str(dates[-1]),
            "initial_capital": 100_000.0,
        },
        "execution": {"fill_model": "perfect"},
    }


# ------------------------------------------------------------------
# _set_nested
# ------------------------------------------------------------------

def test_set_nested_top_level():
    d = {"a": 1}
    _set_nested(d, "a", 99)
    assert d["a"] == 99


def test_set_nested_two_levels():
    d = {"portfolio": {"n_long": 50}}
    _set_nested(d, "portfolio.n_long", 30)
    assert d["portfolio"]["n_long"] == 30


def test_set_nested_missing_path_raises():
    d = {"portfolio": {"n_long": 50}}
    with pytest.raises(KeyError):
        _set_nested(d, "portfolio.missing_key.deeper", 1)


def test_set_nested_does_not_mutate_other_keys():
    d = {"portfolio": {"n_long": 50, "method": "equal_weight"}}
    _set_nested(d, "portfolio.n_long", 30)
    assert d["portfolio"]["method"] == "equal_weight"


# ------------------------------------------------------------------
# _apply_params
# ------------------------------------------------------------------

def test_apply_params_returns_deep_copy():
    cfg = {"portfolio": {"n_long": 50}}
    result = _apply_params(cfg, {"portfolio.n_long": 30})
    assert result["portfolio"]["n_long"] == 30
    assert cfg["portfolio"]["n_long"] == 50  # original unchanged


def test_apply_params_multiple_keys():
    cfg = {"portfolio": {"n_long": 50, "min_holding_days": 21}}
    result = _apply_params(cfg, {"portfolio.n_long": 30, "portfolio.min_holding_days": 0})
    assert result["portfolio"]["n_long"] == 30
    assert result["portfolio"]["min_holding_days"] == 0


# ------------------------------------------------------------------
# ParameterSweeper: structural tests
# ------------------------------------------------------------------

def test_sweep_produces_correct_row_count():
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    result = sweeper.sweep(
        base_config=config,
        param_grid={"portfolio.n_long": [2, 3]},
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    assert result.configs_tested == 2
    assert len(result.rows) == 2


def test_sweep_cartesian_product():
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    result = sweeper.sweep(
        base_config=config,
        param_grid={
            "portfolio.n_long": [2, 3],
            "portfolio.min_holding_days": [0, 5],
        },
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    assert result.configs_tested == 4
    assert len(result.rows) == 4


def test_sweep_result_type():
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    result = sweeper.sweep(
        base_config=config,
        param_grid={"portfolio.n_long": [2, 3]},
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    assert isinstance(result, ParameterSensitivityResult)
    assert result.base_config_name == "test_sweep"


def test_sweep_rows_have_correct_params():
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    result = sweeper.sweep(
        base_config=config,
        param_grid={"portfolio.n_long": [2, 4]},
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    n_long_values = {row.params["portfolio.n_long"] for row in result.rows}
    assert n_long_values == {2, 4}


def test_sweep_metrics_are_finite_or_nan():
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    result = sweeper.sweep(
        base_config=config,
        param_grid={"portfolio.n_long": [2, 3]},
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    for row in result.rows:
        assert math.isfinite(row.oos_sharpe) or math.isnan(row.oos_sharpe)


# ------------------------------------------------------------------
# curve_fit detection
# ------------------------------------------------------------------

def test_empty_param_grid_raises():
    handler = _make_handler()
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    with pytest.raises(ValueError, match="param_grid must not be empty"):
        sweeper.sweep({}, {}, handler)


def test_verdict_is_robust_or_curve_fit():
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    result = sweeper.sweep(
        base_config=config,
        param_grid={"portfolio.n_long": [2, 3]},
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    assert result.verdict in ("robust", "curve_fit")


def test_positive_fraction_between_zero_and_one():
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    result = sweeper.sweep(
        base_config=config,
        param_grid={"portfolio.n_long": [2, 3]},
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    assert 0.0 <= result.positive_fraction <= 1.0


def test_low_positive_fraction_flags_curve_fit():
    """A sweeper with high threshold should flag strategies as curve_fit."""
    sweeper = ParameterSweeper(
        fill_simulator=FillSimulator(fill_model="perfect"),
        min_positive_fraction=0.99,  # nearly impossible to satisfy
    )
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    result = sweeper.sweep(
        base_config=config,
        param_grid={"portfolio.n_long": [2, 3]},
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    assert result.curve_fit_flag is True
    assert result.verdict == "curve_fit"


def test_bad_param_grid_key_raises_key_error():
    """A misspelled dot-path key must raise KeyError, not silently produce NaN."""
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    with pytest.raises(KeyError):
        sweeper.sweep(
            base_config=config,
            param_grid={"portfolio.n_longg": [2, 3]},  # misspelled key
            data_handler=handler,
            n_folds=2,
            train_years=1.5,
            test_months=6,
        )


def test_to_dataframe_columns_and_row_count():
    handler = _make_handler()
    dates = _make_trading_dates(900)
    config = _base_config(dates)
    sweeper = ParameterSweeper(fill_simulator=FillSimulator(fill_model="perfect"))
    result = sweeper.sweep(
        base_config=config,
        param_grid={"portfolio.n_long": [2, 3]},
        data_handler=handler,
        n_folds=2,
        train_years=1.5,
        test_months=6,
    )
    df = result.to_dataframe()
    assert len(df) == 2
    assert "portfolio.n_long" in df.columns
    assert "oos_sharpe" in df.columns
    assert "oos_max_drawdown" in df.columns
    assert "trade_count" in df.columns
    assert "avg_is_sharpe" in df.columns

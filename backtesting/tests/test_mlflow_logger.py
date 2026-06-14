"""Tests for BacktestLogger (MLflow experiment tracking)."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

from backtesting.engine.event_loop import BacktestResult
from backtesting.experiment_tracking.mlflow_logger import BacktestLogger


def _make_result(data_version: str = "snapshot-v1") -> BacktestResult:
    returns = pd.Series([0.001, 0.002, -0.001], index=[date(2023, 1, d) for d in [3, 4, 5]])
    bm_returns = pd.Series([0.001, 0.001, -0.001], index=[date(2023, 1, d) for d in [3, 4, 5]])
    return BacktestResult(
        nav_series=pd.Series([100_000.0, 100_100.0, 100_300.0]),
        returns=returns,
        benchmark_returns=bm_returns,
        positions=pd.DataFrame(),
        trades=pd.DataFrame(),
        metrics={"sharpe": 1.2, "cagr": 0.12, "max_drawdown": -0.05},
        config={"name": "test", "data_version": data_version},
        data_version=data_version,
        config_hash="abc123",
    )


def _make_config(data_version: str = "snapshot-v1") -> dict:
    return {
        "name": "test_strategy",
        "version": 1,
        "data_version": data_version,
        "portfolio": {"n_long": 50},
        "backtest": {"start_date": "2023-01-02", "end_date": "2023-12-31"},
    }


# ------------------------------------------------------------------
# C7 enforcement: data_version must be present
# ------------------------------------------------------------------

def test_log_run_raises_on_empty_data_version():
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result(data_version="")
    config = _make_config(data_version="")
    with pytest.raises(ValueError, match="data_version"):
        logger.log_run(config, result, experiment_name="test/exp")


def test_log_run_raises_on_whitespace_data_version():
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result(data_version="   ")
    config = _make_config(data_version="   ")
    with pytest.raises(ValueError, match="data_version"):
        logger.log_run(config, result, experiment_name="test/exp")


def test_log_run_raises_when_only_config_empty():
    """data_version from BacktestResult takes priority; empty result triggers error."""
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result(data_version="")
    config = _make_config(data_version="snapshot-from-config")
    # result.data_version is empty → should raise
    with pytest.raises(ValueError, match="data_version"):
        logger.log_run(config, result, experiment_name="test/exp")


# ------------------------------------------------------------------
# Successful log (fully mocked MLflow)
# ------------------------------------------------------------------

@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_run_calls_mlflow_set_experiment(mock_mlflow):
    mock_mlflow.start_run.return_value.__enter__ = lambda s: MagicMock(info=MagicMock(run_id="run-abc"))
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result("snapshot-v1")
    config = _make_config("snapshot-v1")

    logger.log_run(config, result, experiment_name="test/exp")
    mock_mlflow.set_experiment.assert_called_once_with("test/exp")


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_run_tags_data_version(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "run-xyz"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result("snapshot-v999")
    config = _make_config("snapshot-v999")

    logger.log_run(config, result, experiment_name="test/exp")

    tag_calls = [str(c) for c in mock_mlflow.set_tag.call_args_list]
    assert any("snapshot-v999" in c for c in tag_calls)


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_run_returns_run_id(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "expected-run-id"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result("v1")
    config = _make_config("v1")

    run_id = logger.log_run(config, result, experiment_name="exp")
    assert run_id == "expected-run-id"

"""Tests for BacktestLogger (MLflow experiment tracking)."""
from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

from backtesting.engine.event_loop import BacktestResult
from backtesting.experiment_tracking.mlflow_logger import BacktestLogger
from backtesting.validation.survival_funnel import FunnelGate, SurvivalFunnelResult


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


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_run_with_funnel_result_logs_gate_tags(mock_mlflow):
    """When funnel_result is supplied, gate verdicts are logged as MLflow tags."""
    run_mock = MagicMock()
    run_mock.info.run_id = "run-funnel"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    funnel = SurvivalFunnelResult(
        passed=False,
        gates=[
            FunnelGate(name="min_oos_sharpe", passed=True, value=0.8, threshold=0.5, description=""),
            FunnelGate(name="min_trade_count", passed=False, value=10.0, threshold=30.0, description=""),
        ],
        verdict="FAIL — gates not cleared: min_trade_count",
    )

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result("v1")
    config = _make_config("v1")
    logger.log_run(config, result, experiment_name="exp", funnel_result=funnel)

    tag_calls = {args[0]: args[1] for args, _ in mock_mlflow.set_tag.call_args_list}
    assert tag_calls.get("survival_funnel.passed") == "False"
    assert tag_calls.get("gate.min_oos_sharpe") == "PASS"
    assert tag_calls.get("gate.min_trade_count") == "FAIL"


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_run_nan_metrics_excluded(mock_mlflow):
    """NaN metric values must not be forwarded to mlflow.log_metric."""
    run_mock = MagicMock()
    run_mock.info.run_id = "run-nan"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    result = BacktestResult(
        nav_series=pd.Series([100_000.0]),
        returns=pd.Series([], dtype=float),
        benchmark_returns=pd.Series([], dtype=float),
        positions=pd.DataFrame(),
        trades=pd.DataFrame(),
        metrics={"sharpe": float("nan"), "cagr": 0.1},
        config={"name": "test", "data_version": "v1"},
        data_version="v1",
        config_hash="abc",
    )
    config = _make_config("v1")
    BacktestLogger(tracking_uri="./test_mlruns").log_run(config, result, experiment_name="exp")

    logged_metrics = [args[0] for args, _ in mock_mlflow.log_metric.call_args_list]
    assert "sharpe" not in logged_metrics
    assert "cagr" in logged_metrics


# ------------------------------------------------------------------
# log_walk_forward_run
# ------------------------------------------------------------------

def _make_wf_result(data_version: str = "v1", n_folds: int = 2) -> MagicMock:
    """Build a minimal WalkForwardResult mock."""
    wf = MagicMock()
    wf.config = {"name": "test_wf", "version": 1, "data_version": data_version}
    wf.oos_metrics = {"sharpe": 0.75, "max_drawdown": -0.12}
    folds = []
    for i in range(n_folds):
        fold = MagicMock()
        fold.in_sample.metrics = {"sharpe": 0.85 + i * 0.05}
        fold.out_of_sample.metrics = {"sharpe": 0.70 + i * 0.05, "max_drawdown": -0.10}
        folds.append(fold)
    wf.folds = folds
    return wf


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_walk_forward_run_raises_on_empty_data_version(mock_mlflow):
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result(data_version="")
    with pytest.raises(ValueError, match="data_version"):
        logger.log_walk_forward_run(
            config=wf.config,
            wf_result=wf,
            experiment_name="test/wf",
        )


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_walk_forward_run_logs_config_hash(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "wf-run-1"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result()
    logger.log_walk_forward_run(config=wf.config, wf_result=wf, experiment_name="exp")

    tag_calls = {args[0]: args[1] for args, _ in mock_mlflow.set_tag.call_args_list}
    assert "config_hash" in tag_calls
    assert len(tag_calls["config_hash"]) == 64  # SHA-256 hex


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_walk_forward_run_logs_per_fold_oos_sharpe(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "wf-run-2"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result(n_folds=2)
    logger.log_walk_forward_run(config=wf.config, wf_result=wf, experiment_name="exp")

    logged_metrics = [args[0] for args, _ in mock_mlflow.log_metric.call_args_list]
    assert "oos.fold_0.sharpe" in logged_metrics
    assert "oos.fold_1.sharpe" in logged_metrics
    assert "is.fold_0.sharpe" in logged_metrics


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_walk_forward_run_with_funnel_and_stress(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "wf-run-3"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    funnel = SurvivalFunnelResult(
        passed=True,
        gates=[FunnelGate("min_oos_sharpe", True, 0.75, 0.5, "")],
        verdict="PASS — strategy cleared all validation gates",
    )
    stress = MagicMock()
    stress.drawdown_p5 = -0.25
    stress.drawdown_p50 = -0.15
    stress.drawdown_p95 = -0.05
    stress.worst_case_drawdown = -0.28
    stress.verdict = "solid"

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result()
    logger.log_walk_forward_run(
        config=wf.config,
        wf_result=wf,
        experiment_name="exp",
        funnel_result=funnel,
        stress_result=stress,
    )

    tag_calls = {args[0]: args[1] for args, _ in mock_mlflow.set_tag.call_args_list}
    assert tag_calls.get("survival_funnel.passed") == "True"
    assert tag_calls.get("gate.min_oos_sharpe") == "PASS"
    assert tag_calls.get("stress.verdict") == "solid"

    logged_metrics = [args[0] for args, _ in mock_mlflow.log_metric.call_args_list]
    assert "stress.drawdown_p5" in logged_metrics
    assert "stress.worst_case_drawdown" in logged_metrics

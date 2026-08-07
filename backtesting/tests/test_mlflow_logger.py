"""Tests for BacktestLogger (MLflow experiment tracking)."""
from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

from backtesting.config_contract import ConfigProvenanceMismatchError
from backtesting.engine.event_loop import BacktestResult
from backtesting.experiment_tracking.mlflow_logger import BacktestLogger, _hash_config
from backtesting.validation.survival_funnel import FunnelGate, SurvivalFunnelResult


def _make_config(data_version: str = "snapshot-v1") -> dict:
    return {
        "name": "test_strategy",
        "version": 1,
        "data_version": data_version,
        "portfolio": {"n_long": 50},
        "backtest": {"start_date": "2023-01-02", "end_date": "2023-12-31"},
    }


def _make_result(data_version: str = "snapshot-v1") -> BacktestResult:
    # config/config_hash must be consistent with what _make_config(same
    # data_version) produces: log_run now fails closed when the passed
    # config's hash differs from result.config_hash (02B round-3
    # provenance check) -- the old fixture ("abc123", a different config
    # dict) modeled exactly the divergence that check closes.
    config = _make_config(data_version)
    returns = pd.Series([0.001, 0.002, -0.001], index=[date(2023, 1, d) for d in [3, 4, 5]])
    bm_returns = pd.Series([0.001, 0.001, -0.001], index=[date(2023, 1, d) for d in [3, 4, 5]])
    return BacktestResult(
        nav_series=pd.Series([100_000.0, 100_100.0, 100_300.0]),
        returns=returns,
        benchmark_returns=bm_returns,
        positions=pd.DataFrame(),
        trades=pd.DataFrame(),
        metrics={"sharpe": 1.2, "cagr": 0.12, "max_drawdown": -0.05},
        config=config,
        data_version=data_version,
        config_hash=_hash_config(config),
    )


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
    """A config declaring a data_version the result lacks can only arise
    from a config/result mismatch, so since the 02B round-3 provenance
    check it fails closed as a provenance error (an honest pair with an
    empty data_version still hits the C7 ValueError -- see the tests
    above)."""
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result(data_version="")
    config = _make_config(data_version="snapshot-from-config")
    with pytest.raises(ConfigProvenanceMismatchError):
        logger.log_run(config, result, experiment_name="test/exp")


# ------------------------------------------------------------------
# 03A-5: require_manifest_data_version transition flag (design plan §2.5's
# last acceptance test) -- opt-in, so it must not affect any of the tests
# above/below that pass legacy-shaped placeholder data_version strings
# without setting the flag.
# ------------------------------------------------------------------

_VALID_MANIFEST_HASH = "a" * 64


def test_log_run_rejects_non_hash_shaped_data_version_when_required():
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result("2026-06-14")  # legacy date-string, not hash-shaped
    config = _make_config("2026-06-14")
    with pytest.raises(ValueError, match="not a manifest-hash-shaped"):
        logger.log_run(
            config,
            result,
            experiment_name="test/exp",
            require_manifest_data_version=True,
        )


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_run_accepts_hash_shaped_data_version_when_required(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "run-hash-ok"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result(_VALID_MANIFEST_HASH)
    config = _make_config(_VALID_MANIFEST_HASH)

    run_id = logger.log_run(
        config,
        result,
        experiment_name="test/exp",
        require_manifest_data_version=True,
    )
    assert run_id == "run-hash-ok"


def test_log_run_rejects_whitespace_padded_hash_when_required():
    """Codex review P2 (round 1): validation must run against the RAW
    data_version (what actually lands in config.json/result.config_hash's
    provenance), not the post-.strip() display value used only for the
    MLflow tag -- otherwise a hash-shaped value with a trailing newline
    (e.g. from a shell $(cat file) capture) would pass the gate on the
    cleaned-up tag while the artifact still carried the untrimmed,
    non-conformant string."""
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    padded = _VALID_MANIFEST_HASH + "\n"
    result = _make_result(padded)
    config = _make_config(padded)
    with pytest.raises(ValueError, match="not a manifest-hash-shaped"):
        logger.log_run(
            config,
            result,
            experiment_name="test/exp",
            require_manifest_data_version=True,
        )


def test_log_run_does_not_enforce_hash_shape_by_default():
    """The transition flag defaults to False: legacy placeholder
    data_version strings used throughout this test module (and, until
    production callers migrate, real callers) must not be rejected unless
    the caller opts in."""
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result("snapshot-v1")
    config = _make_config("snapshot-v1")
    with patch("backtesting.experiment_tracking.mlflow_logger.mlflow"):
        logger.log_run(config, result, experiment_name="test/exp")


def test_log_walk_forward_run_rejects_non_hash_shaped_data_version_when_required():
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result("2026-06-14")
    with pytest.raises(ValueError, match="not a manifest-hash-shaped"):
        logger.log_walk_forward_run(
            config=wf.config,
            wf_result=wf,
            experiment_name="test/wf",
            require_manifest_data_version=True,
        )


def test_log_walk_forward_run_rejects_whitespace_padded_hash_when_required():
    """Codex review P2 (round 1), walk-forward sibling of the log_run fix
    above."""
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result(_VALID_MANIFEST_HASH + "\n")
    with pytest.raises(ValueError, match="not a manifest-hash-shaped"):
        logger.log_walk_forward_run(
            config=wf.config,
            wf_result=wf,
            experiment_name="test/wf",
            require_manifest_data_version=True,
        )


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_walk_forward_run_accepts_hash_shaped_data_version_when_required(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "wf-run-hash-ok"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result(_VALID_MANIFEST_HASH)
    run_id = logger.log_walk_forward_run(
        config=wf.config,
        wf_result=wf,
        experiment_name="exp",
        require_manifest_data_version=True,
    )
    assert run_id == "wf-run-hash-ok"


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

    config = _make_config("v1")
    result = BacktestResult(
        nav_series=pd.Series([100_000.0]),
        returns=pd.Series([], dtype=float),
        benchmark_returns=pd.Series([], dtype=float),
        positions=pd.DataFrame(),
        trades=pd.DataFrame(),
        metrics={"sharpe": float("nan"), "cagr": 0.1},
        config=config,
        data_version="v1",
        config_hash=_hash_config(config),
    )
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


# ------------------------------------------------------------------
# 02B round-3: config provenance checks + reporting.* consumption
# ------------------------------------------------------------------

def test_log_run_rejects_config_hash_provenance_mismatch():
    """Passing a different config than the one the engine ran must fail
    closed -- the persisted config_hash/data_version tags would otherwise
    describe an object other than the one just validated."""
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    result = _make_result("v1")
    divergent = _make_config("v1")
    divergent["portfolio"]["n_long"] = 999  # not what the engine ran
    with pytest.raises(ConfigProvenanceMismatchError):
        logger.log_run(divergent, result, experiment_name="exp")


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_walk_forward_run_rejects_divergent_wf_config(mock_mlflow):
    """wf_result.config diverging from the passed config must fail closed
    (02B round-3 P2-2) -- a swapped wf_result.config could otherwise
    smuggle unvalidated provenance into a validated-looking run."""
    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result("v1")
    passed = dict(wf.config)
    passed["version"] = 999  # diverges from wf_result.config
    with pytest.raises(ConfigProvenanceMismatchError):
        logger.log_walk_forward_run(config=passed, wf_result=wf, experiment_name="exp")


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_walk_forward_run_validates_wf_result_config(mock_mlflow):
    """wf_result.config itself is contract-validated, so an unsupported
    field there cannot ride along even if it matched the passed config."""
    from backtesting.config_contract import UnsupportedStrategyConfigError

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    wf = _make_wf_result("v1")
    wf.config = {**wf.config, "constraints": {"max_portfolio_beta": 1.5}}
    with pytest.raises(UnsupportedStrategyConfigError):
        logger.log_walk_forward_run(config=wf.config, wf_result=wf, experiment_name="exp")


def _result_with_history(config: dict) -> BacktestResult:
    trades = pd.DataFrame({"ticker": ["AAPL"], "direction": ["BUY"], "notional": [100.0]})
    positions = pd.DataFrame(
        {"AAPL": [0.5, 0.5]}, index=[date(2023, 1, 3), date(2023, 1, 4)]
    )
    return BacktestResult(
        nav_series=pd.Series([100_000.0, 100_100.0]),
        returns=pd.Series([0.001], index=[date(2023, 1, 4)]),
        benchmark_returns=pd.Series([0.001], index=[date(2023, 1, 4)]),
        positions=positions,
        trades=trades,
        metrics={"sharpe": 1.0},
        config=config,
        data_version=config["data_version"],
        config_hash=_hash_config(config),
    )


def _logged_artifact_names(mock_mlflow) -> list[str]:
    import os

    return [os.path.basename(args[0]) for args, _ in mock_mlflow.log_artifact.call_args_list]


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_reporting_save_trades_false_skips_trades_artifact(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "r"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    config = _make_config("v1")
    config["reporting"] = {"save_trades": False}
    result = _result_with_history(config)
    BacktestLogger(tracking_uri="./test_mlruns").log_run(config, result, experiment_name="exp")

    names = _logged_artifact_names(mock_mlflow)
    assert "trades.csv" not in names
    assert "positions.csv" not in names  # save_positions defaults False


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_reporting_save_positions_true_writes_positions_artifact(mock_mlflow):
    run_mock = MagicMock()
    run_mock.info.run_id = "r"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    config = _make_config("v1")
    config["reporting"] = {"save_positions": True, "save_trades": True}
    result = _result_with_history(config)
    BacktestLogger(tracking_uri="./test_mlruns").log_run(config, result, experiment_name="exp")

    names = _logged_artifact_names(mock_mlflow)
    assert "positions.csv" in names
    assert "trades.csv" in names


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_reporting_absent_keeps_prior_artifact_behavior(mock_mlflow):
    """No reporting section: trades logged (as before), no positions."""
    run_mock = MagicMock()
    run_mock.info.run_id = "r"
    mock_mlflow.start_run.return_value.__enter__ = lambda s: run_mock
    mock_mlflow.start_run.return_value.__exit__ = lambda *a: False

    config = _make_config("v1")
    result = _result_with_history(config)
    BacktestLogger(tracking_uri="./test_mlruns").log_run(config, result, experiment_name="exp")

    names = _logged_artifact_names(mock_mlflow)
    assert "trades.csv" in names
    assert "positions.csv" not in names


# ------------------------------------------------------------------
# Gate 04 slice 04-4: additive log_promotion_decision (DSR/FDR values).
# Purely additive -- must not touch log_run/log_walk_forward_run at all.
# ------------------------------------------------------------------

@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_promotion_decision_logs_dsr_and_fdr(mock_mlflow):
    client_mock = MagicMock()
    mock_mlflow.tracking.MlflowClient.return_value = client_mock

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    logger.log_promotion_decision(
        run_id="run-abc",
        dsr_value=0.87,
        n_trials=12,
        n_observations=252,
        fdr_rejected=True,
        fdr_alpha=0.05,
    )

    client_mock.log_metric.assert_any_call("run-abc", "dsr.value", 0.87)
    client_mock.log_metric.assert_any_call("run-abc", "dsr.n_trials", 12.0)
    client_mock.log_metric.assert_any_call("run-abc", "dsr.n_observations", 252.0)
    client_mock.set_tag.assert_any_call("run-abc", "fdr.alpha", "0.05")
    client_mock.set_tag.assert_any_call("run-abc", "fdr.rejected", "True")


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_promotion_decision_handles_none_dsr_and_fdr(mock_mlflow):
    """A None dsr_value (DSR could not be computed) must not log a bogus
    dsr.value metric, and a None fdr_rejected (FDR leg skipped) must not
    log an fdr.rejected tag -- both are simply absent rather than a
    sentinel value."""
    client_mock = MagicMock()
    mock_mlflow.tracking.MlflowClient.return_value = client_mock

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    logger.log_promotion_decision(
        run_id="run-xyz",
        dsr_value=None,
        n_trials=3,
        n_observations=100,
        fdr_rejected=None,
    )

    for call_args in client_mock.log_metric.call_args_list:
        assert call_args.args[1] != "dsr.value"
    for call_args in client_mock.set_tag.call_args_list:
        assert call_args.args[1] != "fdr.rejected"
    client_mock.log_metric.assert_any_call("run-xyz", "dsr.n_trials", 3.0)
    client_mock.log_metric.assert_any_call("run-xyz", "dsr.n_observations", 100.0)


@patch("backtesting.experiment_tracking.mlflow_logger.mlflow")
def test_log_promotion_decision_does_not_affect_log_walk_forward_run(mock_mlflow):
    """Additive-only guarantee: calling the new method does not touch
    log_run/log_walk_forward_run's own mlflow.start_run-based call path."""
    client_mock = MagicMock()
    mock_mlflow.tracking.MlflowClient.return_value = client_mock

    logger = BacktestLogger(tracking_uri="./test_mlruns")
    logger.log_promotion_decision(
        run_id="run-additive-only",
        dsr_value=0.5,
        n_trials=1,
        n_observations=10,
    )

    mock_mlflow.start_run.assert_not_called()

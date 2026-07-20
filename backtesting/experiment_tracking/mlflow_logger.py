"""MLflow experiment tracking for backtests.

Enforces C7: every backtest run must supply a non-empty data_version before
it can be logged.  This prevents any result without a traceable data snapshot
from contaminating the experiment registry.

Usage::

    bt_logger = BacktestLogger(tracking_uri="http://localhost:5000")
    run_id = bt_logger.log_run(
        config=config,
        result=result,
        experiment_name="base_momentum/momentum",
    )
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import mlflow
import structlog

from backtesting.config_contract import (
    ConfigProvenanceMismatchError,
    validate_backtest_config,
)
from backtesting.engine.event_loop import BacktestResult

if TYPE_CHECKING:
    from backtesting.validation.survival_funnel import SurvivalFunnelResult
    from backtesting.validation.walk_forward import WalkForwardResult
    from backtesting.validation.bootstrap_stress import BootstrapStressResult

logger = structlog.get_logger(__name__)


class BacktestLogger:
    """Logs backtest runs to MLflow.

    Args:
        tracking_uri: MLflow tracking server URI. Falls back to
            MLFLOW_TRACKING_URI environment variable, then local ./mlruns.
    """

    def __init__(self, tracking_uri: Optional[str] = None) -> None:
        uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI", "mlruns")
        mlflow.set_tracking_uri(uri)
        self._tracking_uri = uri

    def log_run(
        self,
        config: dict,
        result: BacktestResult,
        experiment_name: str,
        run_name: Optional[str] = None,
        tags: Optional[dict[str, str]] = None,
        funnel_result: Optional["SurvivalFunnelResult"] = None,
    ) -> str:
        """Log a backtest result to MLflow.

        Raises ValueError if data_version is empty (C7 compliance).

        Args:
            config: Strategy config dict.
            result: BacktestResult from BacktestEngine.run().
            experiment_name: MLflow experiment name.
            run_name: Optional human-readable run name.
            tags: Optional additional MLflow tags.
            funnel_result: Optional SurvivalFunnelResult; when supplied, each
                gate's pass/fail is logged as an MLflow tag so the validation
                outcome is queryable alongside the run metrics.

        Returns:
            MLflow run_id string.

        Raises:
            UnsupportedStrategyConfigError: ``config`` declares a field,
                section, or value the backtest path does not implement
                (Roadmap 02B / BUG-075, fail-closed -- see
                ``backtesting/config_contract.py``). Checked here too, not
                just at the engine that produced ``result``, so a config
                swapped out between running and logging cannot mislabel a
                persisted MLflow record.
            ConfigProvenanceMismatchError: the passed ``config`` is not the
                config the engine actually ran (``result.config_hash``
                differs from this config's hash) -- the persisted
                ``config_hash``/``data_version`` tags would otherwise be
                derived from a different object than the one just
                validated (02B round-3 sweep).
        """
        validate_backtest_config(config)

        # Provenance check (02B round-3 sweep): everything persisted below
        # -- params, config.json artifact, strategy_name/version tags --
        # comes from the `config` argument just validated, but the
        # data_version and config_hash TAGS come from `result`, which the
        # engine derived from the config it actually ran. If those are two
        # different configs, the record lies about its own provenance.
        # Hash equality implies dict equality (same canonical JSON-SHA256
        # as the engine's _hash_config), which also transitively guarantees
        # config["data_version"] == result.data_version.
        passed_hash = _hash_config(config)
        if passed_hash != result.config_hash:
            raise ConfigProvenanceMismatchError(
                "The config passed to log_run is not the config this "
                "BacktestResult was produced from: hash of the passed "
                f"config is {passed_hash} but result.config_hash is "
                f"{result.config_hash}. Logging would persist "
                "data_version/config_hash tags derived from a different "
                "(possibly unvalidated) config than the one just "
                "validated and recorded in config.json. Pass the exact "
                "config dict the engine ran."
            )

        data_version = (result.data_version or "").strip()
        if not data_version:
            raise ValueError(
                "data_version is required before logging a backtest run (C7). "
                "Set config['data_version'] to the DVC version or MinIO snapshot ID."
            )

        mlflow.set_experiment(experiment_name)

        with mlflow.start_run(run_name=run_name) as run:
            mlflow.set_tag("data_version", data_version)
            mlflow.set_tag("config_hash", result.config_hash)
            mlflow.set_tag("strategy_name", config.get("name", "unknown"))
            mlflow.set_tag("strategy_version", str(config.get("version", "?")))
            if tags:
                mlflow.set_tags(tags)

            _log_params_flat(config)

            for key, value in result.metrics.items():
                if value is None:
                    continue
                if isinstance(value, float) and value != value:  # NaN check
                    continue
                mlflow.log_metric(key, float(value))

            if funnel_result is not None:
                mlflow.set_tag("survival_funnel.passed", str(funnel_result.passed))
                mlflow.set_tag("survival_funnel.verdict", funnel_result.verdict[:250])
                for gate in funnel_result.gates:
                    mlflow.set_tag(
                        f"gate.{gate.name}", "PASS" if gate.passed else "FAIL"
                    )

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)

                config_path = tmp_path / "config.json"
                config_path.write_text(json.dumps(config, indent=2, default=str))
                mlflow.log_artifact(str(config_path), "config")

                if not result.returns.empty:
                    returns_path = tmp_path / "returns.csv"
                    result.returns.to_csv(returns_path, header=True)
                    mlflow.log_artifact(str(returns_path), "data")

                metrics_path = tmp_path / "metrics.json"
                metrics_path.write_text(json.dumps(result.metrics, indent=2, default=str))
                mlflow.log_artifact(str(metrics_path), "data")

                # reporting.* consumption (02B round-3 P2-1): save_trades
                # gates the trades artifact (default True when absent --
                # preserves the previously-unconditional behavior);
                # save_positions gates a positions artifact (default False
                # when absent -- no positions artifact was ever written
                # before, so absence keeps prior behavior).
                reporting_cfg = config.get("reporting") or {}
                save_trades = bool(reporting_cfg.get("save_trades", True))
                save_positions = bool(reporting_cfg.get("save_positions", False))

                if save_trades and not result.trades.empty:
                    trades_path = tmp_path / "trades.csv"
                    result.trades.to_csv(trades_path, index=False)
                    mlflow.log_artifact(str(trades_path), "data")

                if save_positions and not result.positions.empty:
                    positions_path = tmp_path / "positions.csv"
                    result.positions.to_csv(positions_path)
                    mlflow.log_artifact(str(positions_path), "data")

            run_id = run.info.run_id
            logger.info(
                "backtest_logged_to_mlflow",
                run_id=run_id,
                experiment=experiment_name,
                data_version=data_version,
                sharpe=result.metrics.get("sharpe"),
            )
            return run_id

    def log_walk_forward_run(
        self,
        config: dict,
        wf_result: "WalkForwardResult",
        experiment_name: str,
        run_name: Optional[str] = None,
        tags: Optional[dict[str, str]] = None,
        funnel_result: Optional["SurvivalFunnelResult"] = None,
        stress_result: Optional["BootstrapStressResult"] = None,
    ) -> str:
        """Log a walk-forward validation result to MLflow.

        Records OOS metrics, per-fold IS Sharpes, optional survival funnel
        gate results, and optional bootstrap stress drawdown distribution.
        Enforces C7: wf_result.data_version must be non-empty.

        Args:
            config: Strategy config dict.
            wf_result: WalkForwardResult from WalkForwardValidator.run().
            experiment_name: MLflow experiment name.
            run_name: Optional human-readable run name.
            tags: Optional additional MLflow tags.
            funnel_result: Optional SurvivalFunnelResult; gate verdicts logged
                as MLflow tags when supplied.
            stress_result: Optional BootstrapStressResult; drawdown percentiles
                logged as MLflow metrics when supplied.

        Returns:
            MLflow run_id string.

        Raises:
            UnsupportedStrategyConfigError: ``config`` declares a field,
                section, or value the backtest path does not implement
                (Roadmap 02B / BUG-075, fail-closed -- see
                ``backtesting/config_contract.py``).
            ConfigProvenanceMismatchError: ``wf_result.config`` differs
                from the passed ``config`` (02B round-3 P2-2) -- the
                persisted ``data_version``/``config_hash`` are derived
                from ``wf_result.config``, so a mutated or swapped
                ``wf_result.config`` could otherwise smuggle unvalidated
                provenance into a validated-looking run.
        """
        validate_backtest_config(config)

        # Provenance check (02B round-3 P2-2): the tags persisted below mix
        # sources -- data_version and config_hash come from wf_result.config
        # while params and name/version tags come from `config`. Validate
        # wf_result.config too, and require the two to be identical (hash
        # equality of the same canonical serialisation), failing closed on
        # divergence.
        validate_backtest_config(wf_result.config)
        passed_hash = _hash_config(config)
        wf_hash = _hash_config(wf_result.config)
        if passed_hash != wf_hash:
            raise ConfigProvenanceMismatchError(
                "The config passed to log_walk_forward_run differs from "
                f"wf_result.config (hashes {passed_hash} vs {wf_hash}). "
                "data_version/config_hash are persisted from "
                "wf_result.config while params/tags come from the passed "
                "config; logging a divergent pair would record mixed "
                "provenance. Pass the exact config the walk-forward run "
                "was produced from."
            )

        # data_version for walk-forward runs lives in the config dict (via
        # BacktestEngine, which reads config["data_version"] and stores it on
        # BacktestResult).  log_run() reads result.data_version instead, but
        # both ultimately come from the same config field.
        data_version = (wf_result.config.get("data_version") or "").strip()
        if not data_version:
            raise ValueError(
                "data_version is required before logging a walk-forward run (C7). "
                "Set config['data_version'] to the DVC version or MinIO snapshot ID."
            )

        config_hash = _hash_config(wf_result.config)
        mlflow.set_experiment(experiment_name)

        with mlflow.start_run(run_name=run_name) as run:
            mlflow.set_tag("data_version", data_version)
            mlflow.set_tag("config_hash", config_hash)
            mlflow.set_tag("strategy_name", config.get("name", "unknown"))
            mlflow.set_tag("strategy_version", str(config.get("version", "?")))
            mlflow.set_tag("run_type", "walk_forward")
            if tags:
                mlflow.set_tags(tags)

            _log_params_flat(config)

            for key, value in wf_result.oos_metrics.items():
                if value is None:
                    continue
                if not math.isfinite(float(value)):
                    continue
                mlflow.log_metric(f"oos.{key}", float(value))

            for i, fold in enumerate(wf_result.folds):
                is_sharpe = fold.in_sample.metrics.get("sharpe")
                if is_sharpe is not None and math.isfinite(float(is_sharpe)):
                    mlflow.log_metric(f"is.fold_{i}.sharpe", float(is_sharpe))
                oos_sharpe = fold.out_of_sample.metrics.get("sharpe")
                if oos_sharpe is not None and math.isfinite(float(oos_sharpe)):
                    mlflow.log_metric(f"oos.fold_{i}.sharpe", float(oos_sharpe))
                oos_dd = fold.out_of_sample.metrics.get("max_drawdown")
                if oos_dd is not None and math.isfinite(float(oos_dd)):
                    mlflow.log_metric(f"oos.fold_{i}.max_drawdown", float(oos_dd))

            if funnel_result is not None:
                mlflow.set_tag("survival_funnel.passed", str(funnel_result.passed))
                mlflow.set_tag("survival_funnel.verdict", funnel_result.verdict[:250])
                for gate in funnel_result.gates:
                    mlflow.set_tag(
                        f"gate.{gate.name}", "PASS" if gate.passed else "FAIL"
                    )

            if stress_result is not None:
                mlflow.log_metric("stress.drawdown_p5", stress_result.drawdown_p5)
                mlflow.log_metric("stress.drawdown_p50", stress_result.drawdown_p50)
                mlflow.log_metric("stress.drawdown_p95", stress_result.drawdown_p95)
                mlflow.log_metric("stress.worst_case_drawdown", stress_result.worst_case_drawdown)
                mlflow.set_tag("stress.verdict", stress_result.verdict)

            run_id = run.info.run_id
            logger.info(
                "walk_forward_logged_to_mlflow",
                run_id=run_id,
                experiment=experiment_name,
                data_version=data_version,
                oos_sharpe=wf_result.oos_metrics.get("sharpe"),
            )
            return run_id

    def load_result_metrics(self, run_id: str) -> dict:
        """Load the metrics dict for a previously logged run."""
        client = mlflow.tracking.MlflowClient(tracking_uri=self._tracking_uri)
        run = client.get_run(run_id)
        return {k: v.value for k, v in run.data.metrics.items()}


def _log_params_flat(config: dict, prefix: str = "") -> None:
    """Flatten nested config dict and log each leaf as an MLflow param."""
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            _log_params_flat(value, full_key)
        else:
            mlflow.log_param(full_key, str(value)[:500])


def _hash_config(config: dict) -> str:
    """SHA-256 of the canonically serialised config dict."""
    serialised = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode()).hexdigest()

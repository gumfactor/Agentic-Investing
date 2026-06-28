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

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import mlflow
import structlog

from backtesting.engine.event_loop import BacktestResult

if TYPE_CHECKING:
    from backtesting.validation.survival_funnel import SurvivalFunnelResult

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
        """
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

                if not result.trades.empty:
                    trades_path = tmp_path / "trades.csv"
                    result.trades.to_csv(trades_path, index=False)
                    mlflow.log_artifact(str(trades_path), "data")

            run_id = run.info.run_id
            logger.info(
                "backtest_logged_to_mlflow",
                run_id=run_id,
                experiment=experiment_name,
                data_version=data_version,
                sharpe=result.metrics.get("sharpe"),
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

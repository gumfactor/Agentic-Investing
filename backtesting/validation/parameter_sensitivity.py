"""Parameter sensitivity sweep for strategy validation.

Runs the walk-forward validator across a Cartesian product of parameter
variants for a base strategy config.  A strategy that only survives at one
magic parameter setting is a curve-fit artifact; wide, consistent positive
performance across the grid is evidence of a real edge.

Usage::

    from backtesting.validation.parameter_sensitivity import ParameterSweeper

    sweeper = ParameterSweeper()
    result = sweeper.sweep(
        base_config=config,
        param_grid={
            "portfolio.n_long": [30, 50, 75],
            "portfolio.min_holding_days": [0, 21],
        },
        data_handler=handler,
    )
    print(result.verdict)           # "robust" or "curve_fit"
    print(result.positive_fraction) # fraction of variants with OOS Sharpe > 0
    print(result.std_oos_sharpe)    # spread across variants
"""

from __future__ import annotations

import copy
import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd
import structlog

from backtesting.engine.data_handler import DataHandler
from backtesting.engine.event_loop import BacktestEngine
from backtesting.engine.fill_simulator import FillSimulator
from backtesting.validation.walk_forward import WalkForwardValidator
from backtesting.validation.survival_funnel import oos_trade_count_from_wf

logger = structlog.get_logger(__name__)


@dataclass
class ParameterSensitivityRow:
    """Backtest result for one parameter configuration.

    Attributes:
        params: The specific parameter values used for this run, as a dict
            mapping dot-path keys to their values.
        oos_sharpe: Annualised Sharpe on the stitched OOS series.
        oos_max_drawdown: Maximum drawdown on the stitched OOS series.
        trade_count: Total OOS trade count across all folds.
        avg_is_sharpe: Average in-sample Sharpe across folds.
    """

    params: dict[str, Any]
    oos_sharpe: float
    oos_max_drawdown: float
    trade_count: int
    avg_is_sharpe: float


@dataclass
class ParameterSensitivityResult:
    """Aggregate output from a parameter sensitivity sweep.

    Attributes:
        base_config_name: Name field from the base strategy config.
        param_grid: The grid that was swept.
        configs_tested: Total number of parameter combinations run.
        rows: Per-combination results.
        mean_oos_sharpe: Mean OOS Sharpe across valid (finite) variants.
        std_oos_sharpe: Std of OOS Sharpe across valid variants.
            High std means performance is sensitive to parameter choice.
        positive_fraction: Fraction of valid variants with OOS Sharpe > 0.
        curve_fit_flag: True if the strategy failed the robustness checks.
        verdict: "robust" or "curve_fit".
    """

    base_config_name: str
    param_grid: dict[str, list]
    configs_tested: int
    rows: list[ParameterSensitivityRow] = field(default_factory=list)
    mean_oos_sharpe: float = float("nan")
    std_oos_sharpe: float = float("nan")
    positive_fraction: float = 0.0
    curve_fit_flag: bool = False
    verdict: str = "curve_fit"

    def to_dataframe(self) -> "pd.DataFrame":
        """Return per-variant results as a DataFrame for easy analysis.

        Columns: one column per param key, then status ("ok" / "error"),
        oos_sharpe, oos_max_drawdown, trade_count, avg_is_sharpe.  Rows are
        sorted by oos_sharpe descending with errored variants (NaN) at the
        bottom so they are distinguishable from legitimately negative Sharpes.
        """
        records = []
        for row in self.rows:
            record = dict(row.params)
            record["status"] = "ok" if math.isfinite(row.oos_sharpe) else "error"
            record["oos_sharpe"] = row.oos_sharpe
            record["oos_max_drawdown"] = row.oos_max_drawdown
            record["trade_count"] = row.trade_count
            record["avg_is_sharpe"] = row.avg_is_sharpe
            records.append(record)
        df = pd.DataFrame(records)
        if not df.empty:
            df = df.sort_values("oos_sharpe", ascending=False, na_position="last", ignore_index=True)
        return df


class ParameterSweeper:
    """Sweeps a strategy config across a parameter grid and reports Sharpe dispersion.

    Args:
        engine: BacktestEngine instance shared across all sweep runs.
            A new instance is created if not supplied.
        fill_simulator: FillSimulator instance.  A new instance is created
            if not supplied.
        min_positive_fraction: Minimum fraction of parameter variants that
            must produce a positive OOS Sharpe for the strategy to be
            considered robust.  Default 0.5.
        max_sharpe_std: Maximum allowed standard deviation of OOS Sharpe
            across variants.  A high std means only one magic setting drives
            good results.  Default 0.5.
    """

    def __init__(
        self,
        engine: Optional[BacktestEngine] = None,
        fill_simulator: Optional[FillSimulator] = None,
        min_positive_fraction: float = 0.5,
        max_sharpe_std: float = 0.5,
    ) -> None:
        self._engine = engine or BacktestEngine()
        self._fill_sim = fill_simulator or FillSimulator()
        self._min_positive_fraction = min_positive_fraction
        self._max_sharpe_std = max_sharpe_std

    def sweep(
        self,
        base_config: dict,
        param_grid: dict[str, list],
        data_handler: DataHandler,
        n_folds: int = 3,
        window_type: str = "expanding",
        train_years: float = 2.0,
        test_months: int = 12,
    ) -> ParameterSensitivityResult:
        """Run walk-forward validation across all combinations in param_grid.

        Each combination is a deep copy of base_config with the specified
        parameters overridden via dot-path notation (e.g. "portfolio.n_long").
        Variants that fail due to insufficient data or engine errors are logged
        as warnings and recorded with NaN Sharpe; they do not abort the sweep.

        Args:
            base_config: Base strategy config dict.  Deep-copied for each variant.
            param_grid: Dict mapping dot-path config keys to lists of candidate
                values.  E.g. ``{"portfolio.n_long": [30, 50, 75]}``.
                All combinations (Cartesian product) are tested.
            data_handler: Pre-loaded DataHandler covering the full date range.
            n_folds: Walk-forward fold count per run.  Default 3.
            window_type: "expanding" or "rolling".  Default "expanding".
            train_years: Training window length in years.  Default 2.0.
            test_months: OOS test window per fold in months.  Default 12.

        Returns:
            ParameterSensitivityResult with per-variant metrics and summary stats.

        Raises:
            ValueError: If param_grid is empty.
        """
        if not param_grid:
            raise ValueError("param_grid must not be empty.")

        param_keys = list(param_grid.keys())
        combos = list(itertools.product(*param_grid.values()))

        logger.info(
            "parameter_sweep_started",
            strategy=base_config.get("name", "unknown"),
            n_combos=len(combos),
            param_keys=param_keys,
        )

        validator = WalkForwardValidator(
            engine=self._engine,
            fill_simulator=self._fill_sim,
        )

        rows: list[ParameterSensitivityRow] = []
        for combo in combos:
            params = dict(zip(param_keys, combo))
            # _apply_params is outside the try block so that a KeyError from a
            # misspelled dot-path key propagates to the caller rather than being
            # silently recorded as a NaN variant.
            cfg = _apply_params(base_config, params)
            try:
                wf = validator.run(
                    cfg,
                    data_handler,
                    n_folds=n_folds,
                    window_type=window_type,
                    train_years=train_years,
                    test_months=test_months,
                )
                oos_sharpe = float(wf.oos_metrics.get("sharpe", float("nan")))
                oos_dd = float(wf.oos_metrics.get("max_drawdown", float("nan")))
                avg_is = _avg_is_sharpe(wf)
                trade_count = oos_trade_count_from_wf(wf)
            except (ValueError, RuntimeError) as exc:
                # Catch engine-level failures (e.g. insufficient data for a
                # specific param combo) but not configuration errors.
                logger.warning(
                    "parameter_sweep_variant_failed",
                    params=params,
                    error=str(exc),
                )
                oos_sharpe = float("nan")
                oos_dd = float("nan")
                avg_is = float("nan")
                trade_count = 0

            rows.append(ParameterSensitivityRow(
                params=params,
                oos_sharpe=oos_sharpe,
                oos_max_drawdown=oos_dd,
                trade_count=trade_count,
                avg_is_sharpe=avg_is,
            ))

        finite_sharpes = [r.oos_sharpe for r in rows if math.isfinite(r.oos_sharpe)]
        n_valid = len(finite_sharpes)
        mean_sharpe = float(np.mean(finite_sharpes)) if n_valid else float("nan")
        std_sharpe = float(np.std(finite_sharpes, ddof=1)) if n_valid > 1 else 0.0
        pos_frac = float(sum(s > 0 for s in finite_sharpes) / n_valid) if n_valid else 0.0

        if n_valid == 0:
            logger.warning(
                "parameter_sweep_all_variants_failed",
                strategy=base_config.get("name", "unknown"),
                n_combos=len(combos),
            )
        elif n_valid == 1:
            logger.warning(
                "parameter_sweep_single_valid_variant",
                strategy=base_config.get("name", "unknown"),
                detail=(
                    "Only one variant produced a finite OOS Sharpe; the std gate "
                    "is skipped and the positive-fraction gate has no statistical power. "
                    "Expand the param_grid for a meaningful robustness assessment."
                ),
            )

        curve_fit_flag = (
            pos_frac < self._min_positive_fraction
            or (n_valid > 1 and std_sharpe > self._max_sharpe_std)
        )
        verdict = "curve_fit" if curve_fit_flag else "robust"

        logger.info(
            "parameter_sweep_complete",
            strategy=base_config.get("name", "unknown"),
            n_combos=len(combos),
            n_valid=n_valid,
            mean_oos_sharpe=round(mean_sharpe, 3) if math.isfinite(mean_sharpe) else None,
            std_oos_sharpe=round(std_sharpe, 3),
            positive_fraction=round(pos_frac, 3),
            verdict=verdict,
        )

        return ParameterSensitivityResult(
            base_config_name=base_config.get("name", "unknown"),
            param_grid=param_grid,
            configs_tested=len(combos),
            rows=rows,
            mean_oos_sharpe=mean_sharpe,
            std_oos_sharpe=std_sharpe,
            positive_fraction=pos_frac,
            curve_fit_flag=curve_fit_flag,
            verdict=verdict,
        )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _apply_params(base_config: dict, params: dict[str, Any]) -> dict:
    """Return a deep copy of base_config with dot-path params overridden."""
    cfg = copy.deepcopy(base_config)
    for dot_path, value in params.items():
        _set_nested(cfg, dot_path, value)
    return cfg


def _set_nested(d: dict, dot_path: str, value: Any) -> None:
    """Set value at a dot-separated path inside a nested dict in-place."""
    keys = dot_path.split(".")
    node = d
    for key in keys[:-1]:
        if key not in node or not isinstance(node[key], dict):
            raise KeyError(
                f"dot-path '{dot_path}' not found in config at segment '{key}'."
            )
        node = node[key]
    final_key = keys[-1]
    if final_key not in node:
        raise KeyError(
            f"dot-path '{dot_path}' not found in config: terminal key '{final_key}' does not exist."
        )
    node[final_key] = value


def _avg_is_sharpe(wf) -> float:
    sharpes = [
        f.in_sample.metrics.get("sharpe", float("nan"))
        for f in wf.folds
    ]
    return float(np.nanmean(sharpes)) if sharpes else float("nan")

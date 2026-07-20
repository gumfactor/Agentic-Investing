"""Walk-forward out-of-sample validation.

Splits the backtest period into multiple train/test folds and runs the engine
on each fold separately.  Results from the test folds are the out-of-sample
record; train-fold results reveal in-sample fit.

Two window modes:
  expanding – train window grows as folds progress (all history used each time)
  rolling   – train window is a fixed number of years; slides forward each fold
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

import pandas as pd
import structlog

from backtesting.config_contract import validate_backtest_config
from backtesting.engine.data_handler import DataHandler
from backtesting.engine.event_loop import BacktestEngine, BacktestResult
from backtesting.engine.fill_simulator import FillSimulator

logger = structlog.get_logger(__name__)


@dataclass
class WalkForwardFold:
    """Results from one train/test split."""
    fold_number: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    in_sample: BacktestResult
    out_of_sample: BacktestResult


@dataclass
class WalkForwardResult:
    """Aggregate output from a walk-forward run."""
    folds: list[WalkForwardFold]
    oos_returns: pd.Series       # concatenated OOS daily returns
    oos_metrics: dict            # aggregate OOS performance
    config: dict


class WalkForwardValidator:
    """Runs the engine across multiple train/test windows.

    Args:
        engine: BacktestEngine instance to use for each fold.
        fill_simulator: FillSimulator instance (same for all folds).
    """

    def __init__(
        self,
        engine: Optional[BacktestEngine] = None,
        fill_simulator: Optional[FillSimulator] = None,
    ) -> None:
        self._engine = engine or BacktestEngine()
        self._fill_sim = fill_simulator or FillSimulator()

    def run(
        self,
        config: dict,
        data_handler: DataHandler,
        n_folds: int = 3,
        window_type: Literal["expanding", "rolling"] = "expanding",
        train_years: float = 3.0,
        test_months: int = 12,
    ) -> WalkForwardResult:
        """Run walk-forward validation.

        Args:
            config: Strategy config dict.  start_date/end_date are the full
                available range; the validator subdivides it.
            data_handler: Must cover the full date range.
            n_folds: Number of train/test folds.
            window_type: 'expanding' or 'rolling'.
            train_years: Length of the initial/rolling training window.
            test_months: Length of each out-of-sample test window in months.

        Returns:
            WalkForwardResult with per-fold and aggregate OOS metrics.

        Raises:
            UnsupportedStrategyConfigError: ``config`` declares a field,
                section, or value the backtest path does not implement
                (Roadmap 02B / BUG-075, fail-closed -- see
                ``backtesting/config_contract.py``). Raised here, before any
                fold runs, so a rejected config never silently produces
                partial fold results.
        """
        from backtesting.engine.event_loop import _compute_metrics  # avoid circular

        validate_backtest_config(config)

        bt_cfg = config["backtest"]
        full_start = _parse_date(bt_cfg["start_date"])
        full_end = _parse_date(bt_cfg["end_date"])

        all_dates = data_handler.trading_dates(full_start, full_end)
        if not all_dates:
            raise ValueError("No trading dates found in the specified range.")

        folds_dates = _build_fold_dates(
            all_dates, n_folds, train_years, test_months, window_type
        )

        folds: list[WalkForwardFold] = []
        for fold_num, (tr_start, tr_end, te_start, te_end) in enumerate(folds_dates, 1):
            logger.info(
                "walk_forward_fold",
                fold=fold_num,
                train=f"{tr_start} → {tr_end}",
                test=f"{te_start} → {te_end}",
            )
            train_cfg = _config_with_dates(config, tr_start, tr_end)
            test_cfg = _config_with_dates(config, te_start, te_end)

            in_sample = self._engine.run(train_cfg, data_handler, self._fill_sim)
            out_of_sample = self._engine.run(test_cfg, data_handler, self._fill_sim)

            folds.append(WalkForwardFold(
                fold_number=fold_num,
                train_start=tr_start,
                train_end=tr_end,
                test_start=te_start,
                test_end=te_end,
                in_sample=in_sample,
                out_of_sample=out_of_sample,
            ))

        oos_returns = pd.concat(
            [f.out_of_sample.returns for f in folds]
        ).sort_index()

        oos_bm = pd.concat(
            [f.out_of_sample.benchmark_returns for f in folds]
        ).sort_index()

        oos_metrics = _compute_metrics(
            oos_returns,
            oos_bm,
            pd.concat([f.out_of_sample.trades for f in folds]).reset_index(drop=True),
            float(config["backtest"]["initial_capital"]),
        )

        logger.info(
            "walk_forward_complete",
            n_folds=len(folds),
            oos_sharpe=round(oos_metrics.get("sharpe", float("nan")), 3),
            oos_cagr=round(oos_metrics.get("cagr", float("nan")), 4),
        )

        return WalkForwardResult(
            folds=folds,
            oos_returns=oos_returns,
            oos_metrics=oos_metrics,
            config=config,
        )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _build_fold_dates(
    trading_dates: list[date],
    n_folds: int,
    train_years: float,
    test_months: int,
    window_type: str,
) -> list[tuple[date, date, date, date]]:
    """Return (train_start, train_end, test_start, test_end) for each fold."""
    approx_test_days = int(test_months * 21)   # ~21 trading days/month
    approx_train_days = int(train_years * 252)

    total_needed = approx_train_days + n_folds * approx_test_days
    if len(trading_dates) < total_needed:
        raise ValueError(
            f"Insufficient data for {n_folds} folds: need ~{total_needed} days, "
            f"have {len(trading_dates)}."
        )

    folds: list[tuple[date, date, date, date]] = []
    first_date = trading_dates[0]

    for fold in range(n_folds):
        test_start_idx = approx_train_days + fold * approx_test_days
        test_end_idx = min(test_start_idx + approx_test_days - 1, len(trading_dates) - 1)

        if window_type == "expanding":
            tr_start = first_date
        elif window_type == "rolling":
            tr_start_idx = max(0, test_start_idx - approx_train_days)
            tr_start = trading_dates[tr_start_idx]
        else:
            raise ValueError(
                f"Unknown window_type: {window_type!r}. Expected 'expanding' or 'rolling'."
            )

        tr_end = trading_dates[test_start_idx - 1]
        te_start = trading_dates[test_start_idx]
        te_end = trading_dates[test_end_idx]

        folds.append((tr_start, tr_end, te_start, te_end))

    return folds


def _config_with_dates(config: dict, start: date, end: date) -> dict:
    import copy
    cfg = copy.deepcopy(config)
    cfg["backtest"]["start_date"] = str(start)
    cfg["backtest"]["end_date"] = str(end)
    return cfg


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))

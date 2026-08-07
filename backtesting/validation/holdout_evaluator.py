"""``SingleWindowEvaluator`` -- the fixed-config, single-look holdout
confirmation evaluator (Gate 04 slice 04-4W, §2 W2 bug 1 fix,
``docs/plans/04-4W-evaluation-window-threading-scope.md``).

Holdout confirmation is semantically ONE fixed-config backtest over the
sealed holdout window -- the §4.2 one-shot guarantee is "no prior
holdout_confirmation trial row exists", i.e. exactly one look, ever. There is
no walk-forward *within* that one-shot look: a holdout window is typically a
few months (the design doc's worked example is ~6 months), far too short for
``WalkForwardValidator``'s multi-fold split (default ``train_years=3.0`` +
``n_folds=3`` * ``test_months=12`` needs roughly six years of data). Before
this module existed, ``PromotionPipeline.run(holdout_mode=True)`` still
dispatched through ``self._wf_validator`` (the fold-based validator) -- which
either raised ``ValueError("Insufficient data for N folds...")`` against a
realistic holdout window, or, worse, silently degenerated fold subdivision in
a way that never matched "one fixed config, evaluated once."

``SingleWindowEvaluator`` runs exactly one ``BacktestEngine.run()`` call over
the full ``config['backtest']['start_date']..['end_date']`` range and wraps
the single ``BacktestResult`` in a ``WalkForwardResult``-shaped return value
carrying ONE ``WalkForwardFold`` whose ``in_sample`` and ``out_of_sample``
both point at that same ``BacktestResult``. This is a deliberate interface
choice, not an accident:

- It lets ``TrialRecorder.run_walk_forward`` (and therefore its holdout
  guard, one-shot seal, provenance checks, and recording pipeline) dispatch
  to a ``SingleWindowEvaluator`` instance exactly the way it already
  dispatches to a ``WalkForwardValidator`` instance -- ``validator.run(config,
  data_handler, **run_kwargs)`` -- with ZERO change to
  ``trial_recorder.py``'s dispatch mechanics. Only the *object passed in*
  changes (``PromotionPipeline`` selects which evaluator to hand it, based on
  ``holdout_mode``).
- ``SurvivalFunnel.check``'s existing ``avg_is_sharpe_from_wf``/
  ``oos_trade_count_from_wf`` helpers (``backtesting/validation/
  survival_funnel.py``) and ``BacktestLogger.log_walk_forward_run``'s
  per-fold MLflow logging (``backtesting/experiment_tracking/
  mlflow_logger.py``) both read ``WalkForwardResult.folds[i].in_sample``/
  ``.out_of_sample`` -- reusing the same shape means neither needs a
  holdout-specific branch.

There is no genuine in-sample/out-of-sample split in a one-shot holdout
look -- "in_sample" here means "the same single evaluation", not a separate
training partition read from different dates. The ``positive_is_sharpe``
funnel gate therefore reduces, for a holdout confirmation, to "the holdout
evaluation's own Sharpe is positive" -- a reasonable degenerate case, not a
data leak: no dates outside ``[start_date, end_date]`` are ever read.

Explicitly NOT this module's job: subdividing the holdout window into folds,
computing a genuine in-sample/out-of-sample split, or enforcing the §4.2
window/seal guard (``TrialRecorder`` owns that, unchanged, independent of
which evaluator dispatches).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Optional

import structlog

from backtesting.config_contract import validate_backtest_config
from backtesting.engine.data_handler import DataHandler
from backtesting.engine.event_loop import BacktestEngine
from backtesting.engine.fill_simulator import FillSimulator
from backtesting.validation.walk_forward import WalkForwardFold, WalkForwardResult

logger = structlog.get_logger(__name__)


class SingleWindowEvaluator:
    """Runs exactly ONE ``BacktestEngine.run()`` over ``config``'s full date
    range and returns a single-fold ``WalkForwardResult``.

    Deliberately mirrors ``WalkForwardValidator``'s public ``run(config,
    data_handler, **kwargs) -> WalkForwardResult`` signature (accepting and
    ignoring any fold-shaped kwargs such as ``n_folds``/``window_type``/
    ``train_years``/``test_months`` a caller might still pass through
    generically) so the two are interchangeable at
    ``TrialRecorder.run_walk_forward``'s dispatch call site. See the module
    docstring for why this is the smallest-change fix for holdout_mode's
    wrong-evaluator bug.

    Args:
        engine: ``BacktestEngine`` instance to use. Defaults to a fresh
            ``BacktestEngine()``.
        fill_simulator: ``FillSimulator`` instance. Defaults to a fresh
            ``FillSimulator()``.
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
        **_ignored_fold_kwargs: Any,
    ) -> WalkForwardResult:
        """Run one fixed-config backtest over ``config``'s full date range.

        Args:
            config: Strategy config dict. ``config['backtest']['start_date']``/
                ``['end_date']`` define the single window evaluated -- for a
                holdout confirmation this is the registered holdout window
                (``PromotionPipeline`` builds that config before dispatch).
            data_handler: Must cover the evaluated date range.
            **_ignored_fold_kwargs: Accepted and ignored so a caller that
                forwards the same ``walk_forward_kwargs`` it would pass to a
                real ``WalkForwardValidator`` (e.g. ``n_folds``) does not need
                a holdout-specific branch to omit them. There is no fold
                subdivision here -- see the module docstring.

        Returns:
            A ``WalkForwardResult`` with exactly one ``WalkForwardFold``
            whose ``in_sample``/``out_of_sample`` both reference the same
            single ``BacktestResult``, ``oos_returns``/``oos_metrics`` taken
            directly from that result, and ``config`` set to the passed-in
            (dispatched) config -- matching ``WalkForwardValidator.run``'s
            own contract.

        Raises:
            UnsupportedStrategyConfigError: ``config`` declares a field the
                backtest path does not implement (``validate_backtest_config``,
                same as ``WalkForwardValidator.run``/``BacktestEngine.run``).
            ValueError: No trading dates in the configured range (raised by
                ``BacktestEngine.run``; ``TrialRecorder``'s pre-seal preflight
                -- see ``trial_recorder._preflight_holdout_viability`` -- is
                the fail-BEFORE-seal-commits guard for this same condition
                when dispatched via ``final_holdout_confirmation=True``, so in
                practice this should never fire for a properly-preflighted
                holdout run; it remains here as ``BacktestEngine``'s own
                defensive check).
        """
        validate_backtest_config(config)

        bt_cfg = config["backtest"]
        start = _parse_date(bt_cfg["start_date"])
        end = _parse_date(bt_cfg["end_date"])

        result = self._engine.run(config, data_handler, self._fill_sim)

        fold = WalkForwardFold(
            fold_number=1,
            train_start=start,
            train_end=end,
            test_start=start,
            test_end=end,
            # No genuine in-sample/out-of-sample split for a one-shot
            # holdout look -- both legs are the SAME single evaluation (see
            # module docstring). `replace(result)` gives SurvivalFunnel's
            # `.metrics`/`.trades` accessors two independent dataclass
            # instances rather than the identical object under two names,
            # purely so nothing downstream that happens to mutate one leg
            # (none currently does, but the invariant is cheap to hold)
            # silently mutates the other.
            in_sample=result,
            out_of_sample=replace(result),
        )

        logger.info(
            "single_window_evaluation_complete",
            start=str(start),
            end=str(end),
            n_trading_sessions=len(data_handler.trading_dates(start, end)),
            sharpe=round(result.metrics.get("sharpe", float("nan")), 3)
            if result.metrics
            else None,
        )

        return WalkForwardResult(
            folds=[fold],
            oos_returns=result.returns,
            oos_metrics=result.metrics,
            config=config,
        )


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))

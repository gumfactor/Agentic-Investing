"""``TrialRecorder`` — the enforcement layer over the Gate 04 slice 04-1
schema (docs/plans/04-strategy-selection-protocol-design.md §4.1, §4.2, §7
row 04-2).

Wraps :class:`backtesting.validation.walk_forward.WalkForwardValidator` and
:class:`backtesting.validation.parameter_sensitivity.ParameterSweeper` so that
every candidate run against a strategy leaves a durable ``strategy_trials``
row -- inserted with ``status='running'`` *before* the wrapped instrument is
dispatched (so a crashed/discarded run still counts, closing design doc
Gap 1's "just don't report the bad ones" hole) and updated afterwards to
``completed`` (with observed OOS metrics) or ``errored``.

This module also enforces the §4.2 data-split guard: a run whose effective
date range (``config["backtest"]["start_date"]``/``["end_date"]``) overlaps
a registered ``research_data_windows`` holdout window is rejected with
:class:`HoldoutWindowViolationError` *before* the wrapped instrument ever
runs, unless the caller passes ``final_holdout_confirmation=True`` and no
prior ``run_type='holdout_confirmation'`` trial row exists yet for that
``strategy_id`` (the one-shot seal). The app-layer check here is deliberately
redundant with the 04-1 migration's partial unique index
(``uix_strategy_trials_one_holdout_confirmation``) -- the DB index is the
backstop for a bypass of this recorder; this module is the *primary*,
fail-clean rejection path so a caller sees a clear
``HoldoutWindowViolationError`` instead of a raw ``IntegrityError``.

Hybrid mode (design doc §8 Q4, resolved HYBRID): calling
``WalkForwardValidator.run``/``ParameterSweeper.sweep`` directly (unwrapped)
remains permitted for quick exploratory iteration and simply produces no
``strategy_trials`` row -- it is structurally incapable of being cited by a
future ``n_trials`` count or a ``promotion_decisions`` row. ``TrialRecorder``
is the sanctioned entry point for anything that will ever feed a promotion
decision (04-4).

Out of scope for this slice (see docs/plans §7 row 04-2 vs 04-3/04-4/04-5):
``strategy_hypotheses.frozen_at``/``param_grid_json`` immutability
enforcement (04-3), ``PromotionPipeline`` orchestration and ``n_trials``
querying for Deflated Sharpe (04-4), and the ``validated`` Strategy Registry
status wiring (04-5). ``hypothesis_id`` is accepted here purely as a
pass-through FK value; this module does not read or write
``StrategyHypothesis.frozen_at``.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

import numpy as np
import structlog
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backtesting.engine.data_handler import DataHandler
from backtesting.validation.parameter_sensitivity import (
    ParameterSensitivityResult,
    ParameterSweeper,
)
from backtesting.validation.walk_forward import WalkForwardResult, WalkForwardValidator
from strategy_registry.models import Base, StrategyDefinition
from strategy_registry.registry import DefinitionNotFoundError, MissingDataVersionError
from strategy_registry.selection_models import ResearchDataWindow, StrategyTrial

logger = structlog.get_logger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────


class TrialRecorderError(Exception):
    """Base class for TrialRecorder-specific errors."""


class HoldoutWindowViolationError(TrialRecorderError):
    """Raised when a run's date range would touch the sealed holdout window
    without a valid, unconsumed ``final_holdout_confirmation`` request, or
    when a ``final_holdout_confirmation`` request itself is invalid (no
    registered window, range not contained in the holdout window, or the
    one-shot seal has already been consumed for this ``strategy_id``).
    """


# ── TrialRecorder ─────────────────────────────────────────────────────────────


class TrialRecorder:
    """Enforcement wrapper that turns ``WalkForwardValidator``/
    ``ParameterSweeper`` calls into durably-recorded, holdout-guarded trials.

    Follows the same DB-access pattern as
    ``strategy_registry.registry.StrategyRegistry``: one ``create_engine``
    per instance, a SQLite ``PRAGMA foreign_keys=ON`` connect-event when the
    URL is SQLite (so the 04-1 composite FKs and the one-shot-seal partial
    unique index are genuinely enforced in tests), and short-lived
    ``Session`` blocks per DB interaction rather than one long-lived session.
    """

    def __init__(self, db_url: str) -> None:
        self._engine = create_engine(db_url, future=True)
        if db_url.startswith("sqlite"):
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        Base.metadata.create_all(self._engine)

    # ── Public wrapper API ────────────────────────────────────────────────────

    def run_walk_forward(
        self,
        validator: WalkForwardValidator,
        *,
        strategy_id: str,
        config_hash: str,
        data_version: str,
        config: dict,
        data_handler: DataHandler,
        hypothesis_id: Optional[int] = None,
        final_holdout_confirmation: bool = False,
        strategy_family: Optional[str] = None,
        **run_kwargs: Any,
    ) -> WalkForwardResult:
        """Wrap ``validator.run(config, data_handler, **run_kwargs)``.

        Args:
            validator: The ``WalkForwardValidator`` instance to dispatch to
                (or a test double with a compatible ``.run`` method).
            strategy_id: Matches the target ``strategy_definitions`` row.
            config_hash: Matches the target ``strategy_definitions`` row
                (composite FK with ``strategy_id``).
            data_version: Manifest-hash-shaped C7 data version. Required
                (never empty) -- mirrors ``strategy_trials.data_version NOT
                NULL``.
            config: Strategy config dict; ``config["backtest"]["start_date"]``/
                ``["end_date"]`` are read to compute the effective date range
                for the holdout guard.
            data_handler: Passed through to ``validator.run`` unchanged.
            hypothesis_id: Optional FK to a pre-registered
                ``strategy_hypotheses`` row. Not validated/frozen here (04-3
                scope).
            final_holdout_confirmation: One-shot holdout confirmation mode
                (§4.2). When True, ``run_type`` becomes
                ``'holdout_confirmation'`` and ``window`` becomes
                ``'holdout'``; the guard additionally requires the effective
                date range to fall entirely within the registered holdout
                window and that no prior holdout_confirmation trial row
                exists yet for this ``strategy_id``.
            strategy_family: Optional fallback scope for
                ``research_data_windows`` lookup when no per-strategy window
                row exists (§8 Q1: per-strategy is the enforced default; a
                per-family window is still supported as an override).
            **run_kwargs: Forwarded to ``validator.run`` (e.g. ``n_folds``,
                ``window_type``, ``train_years``, ``test_months``).

        Returns:
            The ``WalkForwardResult`` from the wrapped call.

        Raises:
            HoldoutWindowViolationError: The effective date range overlaps a
                registered holdout window without a valid unconsumed
                confirmation request (or the confirmation request itself is
                invalid).
            DefinitionNotFoundError: No ``strategy_definitions`` row exists
                for ``(strategy_id, config_hash)``.
            MissingDataVersionError: ``data_version`` is empty/blank.
            Exception: Any exception the wrapped ``validator.run`` raises is
                recorded (trial marked ``errored``) and re-raised unchanged.
        """
        effective_start, effective_end = _effective_range(config)

        def _dispatch() -> WalkForwardResult:
            return validator.run(config, data_handler, **run_kwargs)

        return self._run_and_record(
            _dispatch,
            strategy_id=strategy_id,
            config_hash=config_hash,
            data_version=data_version,
            effective_start=effective_start,
            effective_end=effective_end,
            hypothesis_id=hypothesis_id,
            final_holdout_confirmation=final_holdout_confirmation,
            strategy_family=strategy_family,
            base_run_type="walk_forward",
            extract_metrics=_extract_walk_forward_metrics,
        )

    def run_parameter_sweep(
        self,
        sweeper: ParameterSweeper,
        *,
        strategy_id: str,
        config_hash: str,
        data_version: str,
        base_config: dict,
        param_grid: dict[str, list],
        data_handler: DataHandler,
        hypothesis_id: Optional[int] = None,
        final_holdout_confirmation: bool = False,
        strategy_family: Optional[str] = None,
        **sweep_kwargs: Any,
    ) -> ParameterSensitivityResult:
        """Wrap ``sweeper.sweep(base_config, param_grid, data_handler,
        **sweep_kwargs)``.

        One ``strategy_trials`` row is recorded per sweep *invocation*
        (``run_type='parameter_sweep_variant'``), not per grid combination --
        the sweep's own ``ParameterSensitivityResult.configs_tested``/``rows``
        already carry the per-combination detail, and that full detail is
        preserved verbatim (sanitized for non-finite floats) in the recorded
        row's ``metrics_json``. See ``run_walk_forward`` for the shared
        holdout-guard and hybrid-mode semantics; the same rules and error
        types apply here.

        Note: per-variant ``ParameterSensitivityResult.rows`` detail is
        intentionally NOT persisted as separate ``strategy_trials`` rows --
        only summary stats (mean/std/positive_fraction/verdict) plus
        ``configs_tested`` are kept in the single recorded row's
        ``metrics_json``. A future 04-4 (``PromotionPipeline``) author should
        not go hunting for per-variant trial rows; they don't exist by
        design (variant configs aren't registered ``strategy_definitions``
        rows, so they can't satisfy the composite ``config_hash`` FK).
        """
        effective_start, effective_end = _effective_range(base_config)

        def _dispatch() -> ParameterSensitivityResult:
            return sweeper.sweep(base_config, param_grid, data_handler, **sweep_kwargs)

        return self._run_and_record(
            _dispatch,
            strategy_id=strategy_id,
            config_hash=config_hash,
            data_version=data_version,
            effective_start=effective_start,
            effective_end=effective_end,
            hypothesis_id=hypothesis_id,
            final_holdout_confirmation=final_holdout_confirmation,
            strategy_family=strategy_family,
            base_run_type="parameter_sweep_variant",
            extract_metrics=_extract_sweep_metrics,
        )

    def list_trials(
        self,
        strategy_id: str,
        run_type: Optional[str] = None,
    ) -> list[StrategyTrial]:
        """Read-only helper: most-recent-first ``strategy_trials`` rows for a
        strategy, optionally filtered by ``run_type``. Useful for tests and
        for 04-4's future ``n_trials`` counting.
        """
        with Session(self._engine) as session:
            q = select(StrategyTrial).where(StrategyTrial.strategy_id == strategy_id)
            if run_type is not None:
                q = q.where(StrategyTrial.run_type == run_type)
            return list(session.scalars(q.order_by(StrategyTrial.started_at.desc())))

    # ── Core recording pipeline ───────────────────────────────────────────────

    def _run_and_record(
        self,
        dispatch: Callable[[], Any],
        *,
        strategy_id: str,
        config_hash: str,
        data_version: str,
        effective_start: date,
        effective_end: date,
        hypothesis_id: Optional[int],
        final_holdout_confirmation: bool,
        strategy_family: Optional[str],
        base_run_type: str,
        extract_metrics: Callable[[Any], tuple[Optional[float], Optional[float], dict]],
    ) -> Any:
        if not data_version or not data_version.strip():
            raise MissingDataVersionError(
                "data_version is required to record a strategy_trials row (C7 discipline). "
                "Pass the MLflow manifest-hash-shaped data_version."
            )

        run_type = "holdout_confirmation" if final_holdout_confirmation else base_run_type
        window = "holdout" if final_holdout_confirmation else "train_oos"

        started_at = datetime.now(tz=timezone.utc)

        # Step 1: enforce the §4.2 holdout guard and insert the 'running' row
        # BEFORE dispatch, in one session, so a rejected run never reaches the
        # wrapped instrument and an accepted run's row exists even if the
        # process crashes mid-dispatch.
        with Session(self._engine) as session:
            self._enforce_holdout_guard(
                session,
                strategy_id=strategy_id,
                effective_start=effective_start,
                effective_end=effective_end,
                final_holdout_confirmation=final_holdout_confirmation,
                strategy_family=strategy_family,
            )

            if session.get(StrategyDefinition, (strategy_id, config_hash)) is None:
                raise DefinitionNotFoundError(
                    f"No definition found for ('{strategy_id}', '{config_hash[:8]}…'). "
                    f"Call StrategyRegistry.add_definition()/register() before recording a trial."
                )

            trial = StrategyTrial(
                strategy_id=strategy_id,
                config_hash=config_hash,
                hypothesis_id=hypothesis_id,
                window=window,
                run_type=run_type,
                data_version=data_version,
                status="running",
                metrics_json={},
                started_at=started_at,
            )
            session.add(trial)
            try:
                session.commit()
            except IntegrityError as integrity_exc:
                session.rollback()
                # A genuine TOCTOU race: two processes both passed
                # _enforce_holdout_guard's app-layer check above before either
                # committed, so the DB's partial unique index
                # (uix_strategy_trials_one_holdout_confirmation) is the
                # backstop that actually catches the second insert. Only
                # relabel this as a clean HoldoutWindowViolationError when it
                # really is the holdout-seal violation -- re-query for the
                # now-committed sibling row to confirm before mislabeling an
                # unrelated IntegrityError (e.g. the FK constraints above).
                if final_holdout_confirmation:
                    existing = session.scalar(
                        select(StrategyTrial).where(
                            StrategyTrial.strategy_id == strategy_id,
                            StrategyTrial.run_type == "holdout_confirmation",
                        )
                    )
                    if existing is not None:
                        raise HoldoutWindowViolationError(
                            f"strategy_id={strategy_id!r} already has a holdout_confirmation "
                            f"trial (id={existing.id}, status={existing.status!r}); a "
                            "concurrent request recorded it first and the one-shot holdout "
                            "seal has already been consumed. A second confirmation requires "
                            "an operator append-only audit correction (C3), never a silent "
                            "retry."
                        ) from integrity_exc
                raise
            session.refresh(trial)
            trial_id = trial.id
            logger.info(
                "strategy_trial_started",
                trial_id=trial_id,
                strategy_id=strategy_id,
                run_type=run_type,
                window=window,
            )

        # Step 2: dispatch OUTSIDE the DB session/transaction. A crash or
        # exception here still leaves the 'running' row from Step 1 (closing
        # Gap 1's "just don't report the bad ones" hole); Step 3/4 below
        # updates it to a terminal status.
        try:
            result = dispatch()
        except Exception as exc:
            try:
                self._mark_errored(trial_id, exc)
            except Exception as mark_exc:
                # The recording write itself failed (e.g. a DB blip while
                # handling the original error). Never let the recording
                # failure mask the original exception's type/message from the
                # caller -- log a distinct event with both exceptions so a
                # human can manually reconcile the trial row stuck in
                # 'running', then re-raise the ORIGINAL exception unchanged.
                logger.error(
                    "strategy_trial_error_recording_failed",
                    trial_id=trial_id,
                    strategy_id=strategy_id,
                    original_error=repr(exc),
                    recording_error=repr(mark_exc),
                )
                raise exc
            logger.warning(
                "strategy_trial_errored",
                trial_id=trial_id,
                strategy_id=strategy_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

        # Step 3: success -- normalize metrics and mark completed.
        oos_sharpe, oos_max_drawdown, metrics = extract_metrics(result)
        self._mark_completed(trial_id, oos_sharpe, oos_max_drawdown, metrics)
        logger.info(
            "strategy_trial_completed",
            trial_id=trial_id,
            strategy_id=strategy_id,
            oos_sharpe=oos_sharpe,
        )
        return result

    def _enforce_holdout_guard(
        self,
        session: Session,
        *,
        strategy_id: str,
        effective_start: date,
        effective_end: date,
        final_holdout_confirmation: bool,
        strategy_family: Optional[str],
    ) -> Optional[ResearchDataWindow]:
        window = self._lookup_window(session, strategy_id, strategy_family)

        if final_holdout_confirmation:
            if window is None:
                raise HoldoutWindowViolationError(
                    f"No research_data_windows row registered for strategy_id={strategy_id!r} "
                    "(or its strategy_family, if supplied); cannot perform a final holdout "
                    "confirmation without a sealed window."
                )
            if not (effective_start >= window.holdout_start and effective_end <= window.holdout_end):
                raise HoldoutWindowViolationError(
                    f"final_holdout_confirmation=True requires the run's effective date "
                    f"range ({effective_start} .. {effective_end}) to fall entirely within "
                    f"the registered holdout window ({window.holdout_start} .. "
                    f"{window.holdout_end}) for strategy_id={strategy_id!r}."
                )
            # One-shot seal: no prior holdout_confirmation trial row of ANY
            # status may exist for this strategy_id (mirrors the 04-1 partial
            # unique index's predicate exactly -- see selection_models.py's
            # comment on why status is not filtered here).
            existing = session.scalar(
                select(StrategyTrial).where(
                    StrategyTrial.strategy_id == strategy_id,
                    StrategyTrial.run_type == "holdout_confirmation",
                )
            )
            if existing is not None:
                raise HoldoutWindowViolationError(
                    f"strategy_id={strategy_id!r} already has a holdout_confirmation trial "
                    f"(id={existing.id}, status={existing.status!r}); the one-shot holdout "
                    "seal has already been consumed and cannot be reused. A second "
                    "confirmation requires an operator append-only audit correction (C3), "
                    "never a silent retry."
                )
            return window

        if window is not None:
            overlaps_holdout = (
                effective_start <= window.holdout_end and effective_end >= window.holdout_start
            )
            if overlaps_holdout:
                raise HoldoutWindowViolationError(
                    f"Run date range ({effective_start} .. {effective_end}) for "
                    f"strategy_id={strategy_id!r} overlaps the registered holdout window "
                    f"({window.holdout_start} .. {window.holdout_end}). Pass "
                    "final_holdout_confirmation=True for the one-shot confirmation run, or "
                    "adjust the config's date range to stay inside train/OOS."
                )
        return window

    def _lookup_window(
        self,
        session: Session,
        strategy_id: str,
        strategy_family: Optional[str],
    ) -> Optional[ResearchDataWindow]:
        # Per-strategy scoping is the operator-confirmed default (§8 Q1); a
        # per-family window is used only as a fallback when no per-strategy
        # row exists and the caller supplied a strategy_family.
        window = session.scalar(
            select(ResearchDataWindow).where(ResearchDataWindow.strategy_id == strategy_id)
        )
        if window is None and strategy_family is not None:
            window = session.scalar(
                select(ResearchDataWindow).where(
                    ResearchDataWindow.strategy_family == strategy_family
                )
            )
        return window

    def _mark_completed(
        self,
        trial_id: int,
        oos_sharpe: Optional[float],
        oos_max_drawdown: Optional[float],
        metrics: dict,
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        with Session(self._engine) as session:
            trial = session.get(StrategyTrial, trial_id)
            if trial is None:  # pragma: no cover - defensive, should not happen
                raise TrialRecorderError(
                    f"strategy_trials row id={trial_id} vanished before its completion update."
                )
            trial.status = "completed"
            trial.oos_sharpe = _normalize_metric(oos_sharpe)
            trial.oos_max_drawdown = _normalize_metric(oos_max_drawdown)
            trial.metrics_json = _sanitize_metrics(metrics)
            trial.completed_at = now
            session.commit()

    def _mark_errored(self, trial_id: int, exc: Exception) -> None:
        now = datetime.now(tz=timezone.utc)
        with Session(self._engine) as session:
            trial = session.get(StrategyTrial, trial_id)
            if trial is None:  # pragma: no cover - defensive, should not happen
                logger.error("strategy_trial_vanished_on_error", trial_id=trial_id)
                return
            trial.status = "errored"
            trial.metrics_json = {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
            trial.completed_at = now
            session.commit()


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _effective_range(config: dict) -> tuple[date, date]:
    bt_cfg = config.get("backtest")
    if not bt_cfg or "start_date" not in bt_cfg or "end_date" not in bt_cfg:
        raise ValueError(
            "config['backtest'] must declare 'start_date' and 'end_date' for the "
            "TrialRecorder holdout guard to compute the effective date range."
        )
    effective_start = _parse_date(bt_cfg["start_date"])
    effective_end = _parse_date(bt_cfg["end_date"])
    if effective_start > effective_end:
        # Fail closed: a reversed range is invalid input to a safety guard
        # and must never silently pass the overlap/containment arithmetic in
        # _enforce_holdout_guard (e.g. `effective_start <= window.holdout_end
        # and effective_end >= window.holdout_start` can spuriously read as
        # "no overlap" for a reversed pair even when the guard should fire).
        raise ValueError(
            f"config['backtest'] date range is reversed: start_date "
            f"({effective_start}) is after end_date ({effective_end})."
        )
    return effective_start, effective_end


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _normalize_metric(value: Optional[Any]) -> Optional[float]:
    """Normalize a non-finite float (NaN/inf) to None before insert -- the
    app-layer commitment from 04-1 (Postgres's NaN CHECK backstop only covers
    Postgres; SQLite relies on this normalization so behavior is identical on
    both backends).
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _sanitize_metrics(obj: Any) -> Any:
    """Recursively normalize non-finite floats (including numpy scalar
    types) to None throughout a metrics dict before it is persisted as
    ``metrics_json`` -- the same NaN/inf -> None commitment as
    ``_normalize_metric``, applied to the full metrics bag rather than just
    the two first-class columns.
    """
    if isinstance(obj, dict):
        return {k: _sanitize_metrics(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_metrics(v) for v in obj]
    if isinstance(obj, np.floating):
        return _normalize_metric(float(obj))
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, float):
        return _normalize_metric(obj)
    return obj


def _extract_walk_forward_metrics(
    result: WalkForwardResult,
) -> tuple[Optional[float], Optional[float], dict]:
    metrics = dict(result.oos_metrics)
    return metrics.get("sharpe"), metrics.get("max_drawdown"), metrics


def _extract_sweep_metrics(
    result: ParameterSensitivityResult,
) -> tuple[Optional[float], Optional[float], dict]:
    metrics = {
        "mean_oos_sharpe": result.mean_oos_sharpe,
        "std_oos_sharpe": result.std_oos_sharpe,
        "positive_fraction": result.positive_fraction,
        "configs_tested": result.configs_tested,
        "verdict": result.verdict,
    }
    return result.mean_oos_sharpe, None, metrics

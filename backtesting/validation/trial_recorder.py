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

This module also enforces a config-provenance guard (P1 fix): before
recording a ``running`` row / dispatching, it recomputes the canonical
``config_hash`` of the config it was actually passed (via
``strategy_registry.fingerprint.hash_config``, the same canonicalisation
``StrategyRegistry``/``BacktestLogger`` already use) and requires it to equal
the caller-supplied ``config_hash`` argument. Without this, a caller could
mutate params/dates while reusing an already-registered ``config_hash`` and
have metrics recorded under the wrong frozen config, corrupting promotion
evidence and Deflated Sharpe trial counts. A mismatch raises
:class:`backtesting.config_contract.ConfigProvenanceMismatchError` (reused
from ``BacktestLogger``'s identical provenance check rather than
reimplemented here). ``data_version`` is excluded from the canonical hash
(``strategy_registry.fingerprint._RUNTIME_KEYS``), so a run whose config
differs from the registered one ONLY in ``data_version`` is still accepted.
For ``run_parameter_sweep``, the BASE config (not any per-variant grid
override) is what was registered and hashed, so the base config is what is
checked against ``config_hash``.

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

from backtesting.config_contract import ConfigProvenanceMismatchError
from backtesting.dataset_manifest import require_manifest_hash_data_version
from backtesting.engine.data_handler import DataHandler
from backtesting.validation.parameter_sensitivity import (
    ParameterSensitivityResult,
    ParameterSweeper,
)
from backtesting.validation.walk_forward import WalkForwardResult, WalkForwardValidator
from strategy_registry.fingerprint import hash_config
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
            ConfigProvenanceMismatchError: The canonical hash of ``config``
                does not equal ``config_hash`` (ignoring ``data_version``).
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
            config=config,
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

        Raises:
            HoldoutWindowViolationError: ``final_holdout_confirmation=True``
                was passed. A parameter sweep evaluates every variant in
                ``param_grid`` against the wrapped data, so a "final holdout
                confirmation" sweep would spend the one-shot sealed-holdout
                look on the *entire grid* rather than a single fixed
                configuration -- exactly the "many looks under one consumed
                seal" failure the §4.2 one-shot guarantee exists to prevent.
                Rejected before any recording or dispatch. A genuine holdout
                confirmation must be a single fixed-config run via
                ``run_walk_forward``.
        """
        if final_holdout_confirmation:
            raise HoldoutWindowViolationError(
                "final_holdout_confirmation=True is not permitted on "
                "run_parameter_sweep(). A parameter sweep evaluates every "
                "variant in param_grid against the wrapped data -- running "
                "it against the sealed holdout window would spend the "
                "one-shot confirmation seal on many looks at the holdout "
                "data instead of one fixed configuration, defeating the "
                "§4.2 one-shot holdout guarantee. Run a single fixed-config "
                "confirmation via TrialRecorder.run_walk_forward() instead."
            )

        effective_start, effective_end = _effective_range(base_config)

        def _dispatch() -> ParameterSensitivityResult:
            return sweeper.sweep(base_config, param_grid, data_handler, **sweep_kwargs)

        # The config-provenance check hashes base_config, not any per-variant
        # override ParameterSweeper.sweep applies internally from param_grid:
        # base_config is what was fingerprinted/registered as this
        # (strategy_id, config_hash) strategy_definitions row, and
        # ParameterSweeper.sweep layers param_grid combinations on top of it
        # per-variant (see parameter_sensitivity.py) -- those per-variant
        # configs are never themselves registered rows (run_parameter_sweep's
        # own docstring above notes they can't satisfy the config_hash FK),
        # so they are not what config_hash could ever validly describe.
        return self._run_and_record(
            _dispatch,
            strategy_id=strategy_id,
            config_hash=config_hash,
            config=base_config,
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
        config: dict,
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
        # C7 (03A-5): a strategy_trials row is promotion evidence, so
        # data_version must be the immutable manifest_content_sha256 shape
        # (64 lowercase hex), not a mutable/legacy token such as a
        # "rqis-snapshots/manifests/{date}/manifest.json" path -- accepting
        # the latter would let a trial row look reproducible while pointing
        # at a token whose underlying manifest can still change. Shares the
        # same helper BacktestLogger uses so both call sites enforce one
        # definition of "hash-shaped".
        require_manifest_hash_data_version(data_version)

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

            # P1 fix: a (strategy_id, config_hash) row existing is not enough
            # -- verify the config actually PASSED to this call is the one
            # that produced config_hash, using the same canonical
            # hashing (key-sorted JSON, data_version stripped) StrategyRegistry
            # and BacktestLogger already use. Without this, a caller could
            # mutate params/dates while reusing an already-registered hash and
            # have metrics recorded under the wrong frozen config_hash,
            # corrupting promotion evidence and DSR trial counts.
            computed_hash = hash_config(config)
            if computed_hash != config_hash:
                raise ConfigProvenanceMismatchError(
                    f"The config passed for strategy_id={strategy_id!r} does not "
                    f"match the claimed config_hash: computed canonical hash of "
                    f"the passed config is {computed_hash} but the caller claims "
                    f"config_hash={config_hash}. Recording a trial here would "
                    "attribute its metrics to a config_hash the passed config "
                    "did not actually produce, corrupting promotion evidence and "
                    "Deflated Sharpe trial counts. Pass the exact config dict "
                    "that was fingerprinted/registered under this config_hash "
                    "(a difference in data_version alone is fine -- it is "
                    "excluded from the canonical hash)."
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
            # P1 fix: a non-confirmation run must be CONTAINED in the
            # registered train/OOS partition [train_start, oos_end], not
            # merely avoid intersecting the holdout window. Rejecting only
            # overlap left a gap: a run with effective_start > holdout_end
            # (post-holdout data) doesn't intersect the holdout window at all,
            # so it would dispatch and be recorded as window='train_oos'
            # without ever consuming the one-shot confirmation seal -- a peek
            # at post-holdout data. Data before train_start is the same class
            # of leak. This containment predicate subsumes the old
            # holdout-overlap rejection for the non-confirmation path: the
            # 04-1 ordering CHECK guarantees holdout_start >= oos_end, so any
            # range overlapping the holdout window already fails
            # effective_end <= window.oos_end.
            contained_in_train_oos = (
                effective_start >= window.train_start and effective_end <= window.oos_end
            )
            if not contained_in_train_oos:
                raise HoldoutWindowViolationError(
                    f"Run date range ({effective_start} .. {effective_end}) for "
                    f"strategy_id={strategy_id!r} is not fully contained within the "
                    f"registered train/OOS partition ({window.train_start} .. "
                    f"{window.oos_end}). This rejects both overlap with the sealed "
                    f"holdout window ({window.holdout_start} .. {window.holdout_end}) "
                    "and any range outside it entirely (before train_start, or after "
                    "oos_end / holdout_end -- a post-holdout peek). Pass "
                    "final_holdout_confirmation=True for the one-shot confirmation "
                    "run within the holdout window, or adjust the config's date "
                    "range to stay inside train/OOS."
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

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

**Class invariant (04-2 round-4 fix):** the guard above only ever inspects
the BASE config's ``backtest.start_date``/``backtest.end_date`` -- it must
validate the exact concrete date range whose data is actually READ during
dispatch, using inclusive-boundary semantics, and must never validate a
declared/base range that dispatch can silently diverge from.
``run_parameter_sweep``'s ``param_grid`` applies dot-path overrides
per-variant (see ``ParameterSweeper._apply_params``/``_set_nested``), and
``_set_nested`` REPLACES the entire subtree at a param_grid key's dot-path --
not just a scalar leaf. So a ``param_grid`` key that is either window leaf
itself (``backtest.start_date``/``backtest.end_date``), OR an ANCESTOR of
one (e.g. the whole ``backtest`` section, which replaces the mapping
containing both dates), could pass the guard on the validated BASE range
while a dispatched VARIANT actually runs over sealed holdout / post-holdout
dates -- recorded under the single ``train_oos`` trial row, with the
one-shot holdout seal never consumed. Per
``backtesting/config_contract.py``'s CONSUMED-field audit, the *only* two
config keys anywhere in the backtest path that control which dates' data
gets read are ``backtest.start_date`` and ``backtest.end_date`` --
everything else CONSUMED (``portfolio.*``, ``execution.*``,
``backtest.initial_capital``, top-level ``name``/``version``/
``data_version``/``strategy_id``) governs strategy parameters, the cost
model, or record labelling, never the window of dates read.
``run_parameter_sweep`` therefore rejects any ``param_grid`` key whose
dot-path is an ancestor-or-equal of either window key's dot-path
(:class:`SweepWindowOverrideError`) *before* any recording or dispatch: the
evaluation window is governed by the registered ``research_data_windows``
row, not the sweep grid -- a sweep varies STRATEGY parameters only, and a
sibling key such as ``backtest.initial_capital`` (not an ancestor of either
date leaf) remains a legitimate sweep target.
Walk-forward fold subdivision (``WalkForwardValidator._build_fold_dates``)
needs no analogous check: every fold date is drawn from
``data_handler.trading_dates(full_start, full_end)``, which is itself
bounded to the validated ``[full_start, full_end]`` range, so folds can
never exceed the outer range the guard already validated (see the assertion
and comment in ``walk_forward.py``).

Hybrid mode (design doc §8 Q4, resolved HYBRID): calling
``WalkForwardValidator.run``/``ParameterSweeper.sweep`` directly (unwrapped)
remains permitted for quick exploratory iteration and simply produces no
``strategy_trials`` row -- it is structurally incapable of being cited by a
future ``n_trials`` count or a ``promotion_decisions`` row. ``TrialRecorder``
is the sanctioned entry point for anything that will ever feed a promotion
decision (04-4).

Out of scope for this slice (see docs/plans §7 row 04-2 vs 04-4/04-5):
``PromotionPipeline`` orchestration and ``n_trials`` querying for Deflated
Sharpe (04-4), and the ``validated`` Strategy Registry status wiring (04-5).

**04-3 addition:** ``frozen_at`` freeze-on-first-trial side effect. When a
trial is recorded with a non-null ``hypothesis_id`` whose linked
``strategy_hypotheses.frozen_at`` is still null, this module sets
``frozen_at`` on that hypothesis row as a side effect of recording the
trial -- in the SAME session/transaction as the ``strategy_trials`` insert
(see ``_run_and_record``'s Step 1 session), so a recorded trial and its
hypothesis freeze can never diverge (both commit together, or neither does).
Subsequent trials linking an already-frozen hypothesis are a no-op on
``frozen_at`` (it is only ever set once). The legacy path where
``hypothesis_id`` is None is unaffected -- no freeze, no requirement.
``param_grid_json`` immutability enforcement itself (rejecting an edit after
``frozen_at`` is set) lives in ``strategy_registry.hypothesis.
HypothesisRegistry.update_param_grid`` -- this module only ever WRITES
``frozen_at``, it never reads or enforces anything about
``param_grid_json``.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

import numpy as np
import structlog
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backtesting.config_contract import ConfigProvenanceMismatchError, validate_backtest_config
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
from strategy_registry.selection_models import (
    ResearchDataWindow,
    StrategyHypothesis,
    StrategyTrial,
)

logger = structlog.get_logger(__name__)


# 04-2 round-5 fix: the two dot-paths anywhere in the backtest path
# (backtesting/engine/event_loop.py, backtesting/validation/walk_forward.py,
# backtesting/validation/parameter_sensitivity.py) that control which dates'
# data get read. Derived directly from the CONSUMED-field audit in
# backtesting/config_contract.py: every other CONSUMED field
# (portfolio.n_long, portfolio.method, portfolio.rebalance_frequency,
# portfolio.min_holding_days, portfolio.max_position_weight, execution.*,
# backtest.initial_capital, top-level name/version/data_version/strategy_id)
# governs strategy construction, cost model, or record labelling within an
# already-fixed date range -- never the range itself.
#
# The invariant enforced by ``_reject_window_override_keys`` is NOT "the
# param_grid key is exactly one of these two strings" -- ``ParameterSweeper.
# _set_nested`` applies a param_grid key as a dot-path and REPLACES the
# entire subtree at that path (see parameter_sensitivity.py's
# ``_set_nested``/``_apply_params``). So a param_grid key of ``"backtest"``
# (the whole section, one segment up from either leaf) also replaces
# ``backtest.start_date``/``backtest.end_date`` wholesale -- it just does so
# indirectly, by overwriting their parent mapping. The correct rejection
# rule is therefore ANCESTRY: reject a key ``k`` iff overriding the subtree
# rooted at ``k`` could change either window key's value, i.e. iff ``k``'s
# dot-path segments are a prefix of (or equal to) ``["backtest",
# "start_date"]`` or ``["backtest", "end_date"]``. A degenerate empty/blank
# key is rejected defensively as an ancestor of everything (a root-level
# replace). Sibling keys such as ``backtest.initial_capital`` are NOT
# ancestors of either date leaf and remain allowed, so legitimate
# non-window sweeps over that key still work. See the module docstring's
# "class invariant" note.
_WINDOW_KEY_PATHS: tuple[tuple[str, ...], ...] = (
    ("backtest", "start_date"),
    ("backtest", "end_date"),
)


def _is_window_overriding_key(key: str) -> bool:
    """True iff replacing the subtree at dot-path ``key`` (as ``ParameterSweeper.
    _set_nested`` does for a ``param_grid`` entry) could change
    ``backtest.start_date`` or ``backtest.end_date`` -- i.e. ``key``'s segments
    are an ancestor-or-equal dot-path of either window key (04-2 round-5 fix).
    """
    stripped = key.strip()
    if not stripped:
        # A blank/whitespace-only key is a degenerate ancestor of the whole
        # config root -- reject defensively rather than let it fall through
        # to whatever _set_nested does with an empty path.
        return True
    segments = tuple(stripped.split("."))
    for window_path in _WINDOW_KEY_PATHS:
        if segments == window_path[: len(segments)]:
            return True
    return False


def _reject_window_override_keys(param_grid: dict[str, list]) -> None:
    """Fail closed before any recording/dispatch if ``param_grid`` contains a
    dot-path key that is an ancestor-or-equal of ``backtest.start_date``/
    ``backtest.end_date`` -- i.e. a key whose per-variant override could move
    the window of dates whose data is actually read (04-2 round-5 fix).
    """
    offending = sorted(key for key in param_grid if _is_window_overriding_key(key))
    if offending:
        raise SweepWindowOverrideError(
            "param_grid contains key(s) whose per-variant override could "
            f"change the evaluation date window: {offending}. A parameter "
            "sweep varies STRATEGY parameters only -- the evaluation window "
            "is governed by the registered research_data_windows row "
            "(validated against base_config's backtest.start_date/end_date), "
            "never by the sweep grid. ParameterSweeper._set_nested replaces "
            "the entire subtree at a param_grid key's dot-path, so a key "
            "that is an ancestor of backtest.start_date/backtest.end_date "
            "(e.g. the whole 'backtest' section, not just the leaf date "
            "keys themselves) can also move the window indirectly. Allowing "
            "that would let a variant run over sealed holdout / "
            "post-holdout dates while the single trial row is recorded as "
            "'train_oos' and the one-shot holdout seal is never consumed. "
            "Remove these key(s) from param_grid; if you need to test a "
            "different date range, run a separate TrialRecorder call with "
            "that range as the base config so the guard validates what "
            "actually executes."
        )


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


class SweepWindowOverrideError(TrialRecorderError):
    """Raised when a ``run_parameter_sweep`` ``param_grid`` contains a
    dot-path key that is an ancestor-or-equal of ``backtest.start_date``/
    ``backtest.end_date`` -- i.e. a key whose per-variant override could move
    the window of dates whose data is actually read, diverging from the base
    range the §4.2 holdout guard validated (04-2 round-5 fix).

    See the module docstring's "class invariant" note for the full
    rationale: the rejection rule is dot-path ANCESTRY relative to
    ``backtest.start_date``/``backtest.end_date`` (so the whole ``backtest``
    section is rejected too), not exact membership in those two leaf keys.
    """


class DataVersionProvenanceMismatchError(TrialRecorderError):
    """Raised when ``config`` already declares a non-empty top-level
    ``data_version`` that disagrees with the ``data_version`` argument the
    caller is asking this trial to be recorded under (04-2 round-2 P2 fix).

    ``hash_config`` strips ``data_version`` from the canonical
    ``config_hash`` (it is a ``_RUNTIME_KEYS`` entry), so the config-hash
    provenance check above cannot catch this: a caller could pass a
    ``config['data_version']`` that differs from (or is blank while the
    dispatched backtest ends up reading) the validated ``data_version``
    argument, leaving the ``strategy_trials`` row's C7 manifest hash
    inconsistent with what ``BacktestEngine.run``/``BacktestLogger`` actually
    recorded for the dispatched backtest. A caller should not silently
    disagree with itself about which data snapshot a trial ran against.
    """


class HoldoutEvaluationPreflightError(TrialRecorderError):
    """Raised by the pre-seal viability preflight (04-4W W2 bug 2 fix) when a
    ``final_holdout_confirmation=True`` request cannot actually be evaluated
    -- e.g. the config fails ``validate_backtest_config`` or the
    ``data_handler`` reports too few trading sessions in the effective
    holdout range.

    This is deliberately raised BEFORE the seal-committing ``strategy_trials``
    insert (see ``_preflight_holdout_viability`` and ``run_walk_forward``'s
    call site), so a SETUP failure -- a config or data problem discovered
    before any holdout return was ever read -- never consumes the one-shot
    holdout seal (``uix_strategy_trials_one_holdout_confirmation``). Compare
    with ``HoldoutWindowViolationError``, which is raised for genuine policy
    violations (wrong window, seal already consumed) rather than "this
    attempt was never going to be able to run." A caller that fixes the
    underlying config/data problem and retries is not performing a
    disallowed second look -- the first attempt never got to look at all.
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
            DataVersionProvenanceMismatchError: ``config`` already declares a
                non-empty top-level ``data_version`` that disagrees with the
                ``data_version`` argument.
            MissingDataVersionError: ``data_version`` is empty/blank.
            HoldoutEvaluationPreflightError: ``final_holdout_confirmation=True``
                and the pre-seal viability preflight determines the holdout
                window cannot actually be evaluated (config fails
                ``validate_backtest_config``, or ``data_handler`` reports too
                few trading sessions in the effective range). Raised BEFORE
                the seal-committing ``strategy_trials`` insert (04-4W W2 bug
                2 fix), so this SETUP failure never consumes the one-shot
                holdout seal.
            Exception: Any exception the wrapped ``validator.run`` raises is
                recorded (trial marked ``errored``) and re-raised unchanged.
        """
        effective_start, effective_end = _effective_range(config)

        def _dispatch(dispatch_config: dict) -> WalkForwardResult:
            return validator.run(dispatch_config, data_handler, **run_kwargs)

        # 04-4W W2 bug 2 fix: a data-sufficiency + config-viability preflight
        # for a final_holdout_confirmation=True request, invoked by
        # _run_and_record AFTER _enforce_holdout_guard's policy checks
        # (window registered, range contained, seal not already consumed)
        # but BEFORE the seal-committing 'running' row insert -- both still
        # inside the same DB session/transaction as the guard. Ordering
        # after the guard means a policy violation (e.g. range not
        # contained in the registered window) still raises
        # HoldoutWindowViolationError as before, even against a data_handler
        # that would also fail the viability check; the viability check only
        # matters once the request has already cleared policy. Without this
        # hook, a setup/insufficient-data error inside the wrapped
        # validator.run (dispatched AFTER the insert, in Step 2) would still
        # have already burned the one-shot seal, permanently blocking even a
        # corrected retry. See _preflight_holdout_viability's docstring for
        # the exact "availability/shape only, never a look at returns"
        # boundary this preflight is not allowed to cross. Scoped to
        # final_holdout_confirmation only -- a train_oos run consumes no
        # seal, so there is nothing here for it to prematurely burn.
        pre_insert_check = (
            (lambda: _preflight_holdout_viability(config, data_handler))
            if final_holdout_confirmation
            else None
        )

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
            pre_insert_check=pre_insert_check,
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
            SweepWindowOverrideError: ``param_grid`` contains a dot-path key
                that is an ancestor-or-equal of ``backtest.start_date``/
                ``backtest.end_date`` (e.g. either leaf itself, or the whole
                ``backtest`` section) that would let a per-variant override
                move the window of dates whose data is actually read,
                diverging from the base range the §4.2 guard validated
                (04-2 round-5 fix). Checked first, before any recording or
                dispatch.
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
        # 04-2 round-4 fix: reject before any recording/dispatch -- see
        # _reject_window_override_keys and the module docstring's "class
        # invariant" note.
        _reject_window_override_keys(param_grid)

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

        # Planned variant count (Cartesian product of param_grid), computed
        # BEFORE dispatch from the grid shape alone. Seeded into the initial
        # 'running' row's metrics_json['configs_tested'] so a crashed/errored
        # sweep still carries an honest attempted count for
        # PromotionPipeline._compute_n_trials, instead of falling back to 1.
        # None (malformed/empty grid) means "leave it unset" -- the existing
        # _compute_n_trials fallback+warning already covers a missing value.
        planned_configs_tested = _planned_grid_size(param_grid)
        initial_metrics_json = (
            {"configs_tested": planned_configs_tested}
            if planned_configs_tested is not None
            else None
        )

        def _dispatch(dispatch_config: dict) -> ParameterSensitivityResult:
            return sweeper.sweep(dispatch_config, param_grid, data_handler, **sweep_kwargs)

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
            initial_metrics_json=initial_metrics_json,
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
        dispatch: Callable[[dict], Any],
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
        initial_metrics_json: Optional[dict] = None,
        pre_insert_check: Optional[Callable[[], None]] = None,
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

            # 04-4W W2 bug 2 fix: run the data-sufficiency + config-viability
            # preflight (when supplied -- only run_walk_forward's
            # final_holdout_confirmation=True path supplies one) AFTER the
            # policy guard above but BEFORE anything below that leads to the
            # seal-committing insert. A policy violation (wrong/unregistered
            # window, seal already consumed) is raised by the guard first,
            # unaffected by data availability; only a request that already
            # clears policy is then checked for whether it can actually be
            # evaluated. See _preflight_holdout_viability's docstring for the
            # precise "availability/shape only" boundary it must not cross.
            if pre_insert_check is not None:
                pre_insert_check()

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
                    "(differences confined to the evaluation-context keys "
                    "excluded from the identity hash are fine: data_version and "
                    "backtest.start_date/backtest.end_date -- the latter is what "
                    "lets a holdout confirmation reuse the frozen winner's "
                    "config_hash while swapping in the holdout window; see "
                    "docs/plans/04-identity-evaluation-context-design.md)."
                )

            # 04-2 round-2 P2 fix: hash_config strips the evaluation-context
            # keys (data_version + backtest.start_date/end_date) from the
            # identity hash, so the check above deliberately lets a config
            # differ from the registered one in those keys through -- but that
            # means it cannot catch a caller whose
            # config['data_version'] disagrees with the data_version argument
            # this trial row is about to record. Reject that disagreement
            # explicitly rather than silently recording one C7 version while
            # dispatching a config that carries (or later gets overwritten
            # to carry) a different one.
            existing_config_data_version = config.get("data_version")
            if (
                existing_config_data_version
                and str(existing_config_data_version).strip()
                and existing_config_data_version != data_version
            ):
                raise DataVersionProvenanceMismatchError(
                    f"config['data_version'] ({existing_config_data_version!r}) for "
                    f"strategy_id={strategy_id!r} does not match the data_version "
                    f"argument ({data_version!r}) this trial is being recorded "
                    "under. Pass the same data_version in both places, or omit "
                    "config['data_version'] and let TrialRecorder set it from the "
                    "validated argument."
                )

            # Dispatch a COPY of config carrying the validated data_version at
            # the top level -- never mutate the caller's dict in place.
            # BacktestEngine.run reads config.get("data_version", "") directly
            # (backtesting/engine/event_loop.py), and both
            # WalkForwardValidator._config_with_dates and
            # ParameterSweeper._apply_params deep-copy this same dict
            # per-fold/per-variant, so setting it here on the copy that is
            # actually dispatched is what keeps WalkForwardResult.config,
            # every per-fold/per-variant BacktestResult.data_version, and any
            # later BacktestLogger.log_run/log_walk_forward_run call
            # consistent with the data_version this strategy_trials row
            # records below. Injected after the hash_config provenance check
            # above (which already ignores data_version) so it cannot change
            # that comparison's outcome.
            dispatch_config = _config_with_data_version(config, data_version)

            # 04-3: freeze-on-first-trial side effect. If this trial links a
            # hypothesis (hypothesis_id is not None) whose param_grid_json is
            # still mutable (frozen_at IS NULL), set frozen_at now, in THIS
            # session -- the same transaction as the strategy_trials insert
            # below (both share one session.commit() call a few lines down),
            # so a committed trial and its hypothesis freeze can never
            # diverge. If the hypothesis row does not exist at all, leave it
            # alone here: the composite FK
            # (hypothesis_id, strategy_id) -> strategy_hypotheses(id,
            # strategy_id) already rejects that case at commit time with an
            # IntegrityError, unchanged from 04-2's behavior. An
            # already-frozen hypothesis (frozen_at IS NOT NULL) is a no-op --
            # frozen_at is set exactly once, on the first linked trial, and
            # every subsequent trial linking it proceeds without altering it.
            # Capture whether THIS call applied the freeze so the audit log
            # line (04-3 P3 fix) can be emitted only after the commit below
            # actually succeeds -- logging here, before commit, would record
            # "frozen" for a freeze that a later IntegrityError rolls back
            # (e.g. the trial insert failing the composite FK / holdout
            # seal), leaving an audit event that never took effect. The
            # freeze assignment itself stays in-session/atomic with the
            # trial insert; only the LOG EMISSION moves to after commit.
            # Freeze the linked hypothesis with an ATOMIC conditional UPDATE,
            # not a read-then-write (Codex round-1 P2; same TOCTOU class as
            # HypothesisRegistry.update_param_grid). Two concurrent trials for
            # the same unfrozen hypothesis could otherwise both read
            # frozen_at IS NULL and both write, violating the set-once
            # invariant and double-emitting the audit event; the guard must
            # live in the UPDATE predicate. The UPDATE runs in THIS session,
            # so it stays atomic with the strategy_trials insert (one commit
            # covers both, or neither lands). rowcount == 1 means THIS call
            # won the set-once race and applied the freeze; 0 means the
            # hypothesis was already frozen, or does not exist -- the latter
            # is rejected at commit by the trial's composite FK, unchanged.
            freeze_applied = False
            if hypothesis_id is not None:
                freeze_result = session.execute(
                    update(StrategyHypothesis.__table__)
                    .where(
                        StrategyHypothesis.__table__.c.id == hypothesis_id,
                        StrategyHypothesis.__table__.c.frozen_at.is_(None),
                    )
                    .values(frozen_at=started_at)
                )
                freeze_applied = freeze_result.rowcount == 1

            trial = StrategyTrial(
                strategy_id=strategy_id,
                config_hash=config_hash,
                hypothesis_id=hypothesis_id,
                window=window,
                run_type=run_type,
                data_version=data_version,
                status="running",
                metrics_json=dict(initial_metrics_json) if initial_metrics_json else {},
                started_at=started_at,
                # 04-4W W1 fix: persist the EFFECTIVE evaluation range this
                # trial was already validated/guarded against above --
                # previously computed and used by _enforce_holdout_guard,
                # then discarded rather than recorded on the row it gates.
                # Once config_hash stopped covering backtest.start_date/
                # backtest.end_date, this is what makes a train_oos trial
                # and a holdout trial recorded under the SAME config_hash
                # distinguishable from the row alone (see
                # strategy_registry/selection_models.py's StrategyTrial
                # docstring and migration 015).
                eval_start_date=effective_start,
                eval_end_date=effective_end,
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
            # Commit succeeded -- if this call applied the freeze, the audit
            # event now reflects reality (the freeze is durable, not a
            # since-rolled-back side effect).
            if freeze_applied:
                logger.info(
                    "strategy_hypothesis_frozen",
                    hypothesis_id=hypothesis_id,
                    strategy_id=strategy_id,
                )
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
            result = dispatch(dispatch_config)
        except Exception as exc:
            try:
                self._mark_errored(trial_id, exc, initial_metrics_json=initial_metrics_json)
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
            #
            # 04-2 round-2 P1 fix: the 04-1 window-ordering CHECK permits
            # oos_end == holdout_start (touching partitions), and backtest
            # date ranges are INCLUSIVE on both ends (DataHandler.trading_dates
            # returns dates in [start, end]). So `effective_end <= window.oos_end`
            # alone still let a run whose effective_end lands exactly on a
            # touching oos_end/holdout_start boundary dispatch and be recorded
            # as window='train_oos' -- reading the FIRST sealed holdout session
            # without ever consuming the one-shot confirmation seal. Add an
            # explicit `effective_end < window.holdout_start` clause so the
            # upper edge is always strictly before the holdout window, whether
            # or not there is a gap. When oos_end < holdout_start (a gap), this
            # clause is already implied by `effective_end <= window.oos_end`
            # and is a no-op; when oos_end == holdout_start (touching), this
            # clause is what actually excludes the shared boundary date. Both
            # clauses are kept so the intent -- contained in train/OOS AND
            # strictly before the holdout seal -- is explicit rather than
            # relying on an accidental implication.
            contained_in_train_oos = (
                effective_start >= window.train_start
                and effective_end <= window.oos_end
                and effective_end < window.holdout_start
            )
            if not contained_in_train_oos:
                raise HoldoutWindowViolationError(
                    f"Run date range ({effective_start} .. {effective_end}) for "
                    f"strategy_id={strategy_id!r} is not fully contained within the "
                    f"registered train/OOS partition ({window.train_start} .. "
                    f"{window.oos_end}), or its end touches/crosses the sealed "
                    f"holdout window start ({window.holdout_start}). The allowed "
                    f"partition requires effective_start >= {window.train_start}, "
                    f"effective_end <= {window.oos_end}, AND effective_end strictly "
                    f"before {window.holdout_start} (dates are inclusive, so a run "
                    "ending exactly on a touching oos_end/holdout_start boundary "
                    "would otherwise read the first sealed holdout session). This "
                    "rejects overlap with the sealed holdout window "
                    f"({window.holdout_start} .. {window.holdout_end}), any range "
                    "outside the partition entirely (before train_start, or after "
                    "oos_end / holdout_end -- a post-holdout peek), and a run "
                    "touching the holdout boundary. Pass "
                    "final_holdout_confirmation=True for the one-shot confirmation "
                    "run within the holdout window, or adjust the config's date "
                    "range to stay inside train/OOS and strictly before "
                    "holdout_start."
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

    def _mark_errored(
        self,
        trial_id: int,
        exc: Exception,
        *,
        initial_metrics_json: Optional[dict] = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        with Session(self._engine) as session:
            trial = session.get(StrategyTrial, trial_id)
            if trial is None:  # pragma: no cover - defensive, should not happen
                logger.error("strategy_trial_vanished_on_error", trial_id=trial_id)
                return
            trial.status = "errored"
            # Preserve the planned-count seed (e.g. configs_tested) recorded
            # at insert time -- an errored sweep still carries its attempted
            # variant count alongside the error detail, rather than losing it
            # to a full metrics_json overwrite.
            metrics_json = dict(initial_metrics_json) if initial_metrics_json else {}
            metrics_json.update(
                {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            trial.metrics_json = metrics_json
            trial.completed_at = now
            session.commit()


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _planned_grid_size(param_grid: dict[str, list]) -> Optional[int]:
    """Cartesian-product size of ``param_grid`` -- the number of variants a
    full sweep is PLANNED to execute, computed up front from the grid shape
    alone (no dispatch required). Used to seed ``metrics_json['configs_tested']``
    on the initial ``running`` row so a crashed/errored sweep still carries an
    honest attempted count instead of silently understating ``n_trials`` (see
    ``PromotionPipeline._compute_n_trials``).

    Returns ``None`` (leave unset) for a malformed or empty grid -- e.g. a
    non-dict, an empty dict, or any value that isn't a non-empty list-like of
    candidates -- rather than raising, since a malformed grid is a pre-existing
    validation concern of ``ParameterSweeper.sweep`` itself, not something this
    seeding helper should crash on.
    """
    if not isinstance(param_grid, dict) or not param_grid:
        return None
    size = 1
    for value in param_grid.values():
        try:
            n = len(value)
        except TypeError:
            return None
        if n <= 0:
            return None
        size *= n
    return size


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


# 04-4W W2 bug 2 fix: minimum trading sessions the preflight requires in the
# effective holdout range before it will let a final_holdout_confirmation
# request proceed to the seal-committing insert. A single-window evaluation
# (backtesting.validation.holdout_evaluator.SingleWindowEvaluator) computes
# daily returns via nav_series.pct_change().dropna() -- one trading session
# produces zero usable return rows (the diff has nothing to compare against),
# which would insert a 'running' row, consume the one-shot seal, and then
# hand SurvivalFunnel/bootstrap_stress an all-empty oos_returns series. Two
# is the minimum that can ever produce a single non-NaN daily return.
_MIN_HOLDOUT_TRADING_SESSIONS = 2


def _preflight_holdout_viability(config: dict, data_handler: "DataHandler") -> None:
    """Fail-closed data-sufficiency + config-viability check for a
    ``final_holdout_confirmation=True`` request, run BEFORE the seal-
    committing ``strategy_trials`` insert (04-4W W2 bug 2 fix).

    **Boundary this function must never cross**: it may only check the
    AVAILABILITY/SHAPE of the holdout window's data -- that
    ``config['backtest']`` is a structurally valid backtest config
    (``validate_backtest_config``) and that ``data_handler`` reports at
    least ``_MIN_HOLDOUT_TRADING_SESSIONS`` trading dates in the effective
    range (``DataHandler.trading_dates``, a plain date-index lookup) -- it
    must NEVER read a price, a return, or any metric that would constitute
    "a look" at the holdout evaluation itself. The one-shot seal exists
    precisely because reading holdout RETURNS is the thing that may happen
    at most once, ever; reading the date INDEX shape is not a look at
    returns and is safe to do before the seal commits. This mirrors
    ``WalkForwardValidator.run``'s own pre-dispatch
    ``data_handler.trading_dates(...)`` emptiness check
    (``backtesting/validation/walk_forward.py``) -- the same class of setup
    check, just performed here, before the recorder's insert, so a bad
    holdout window fails BEFORE the trial row (and therefore the seal)
    commits, instead of after.

    A run that clears this preflight and then fails DURING evaluation (e.g.
    inside ``BacktestEngine.run``, after the 'running' row is already
    inserted and dispatch has started) still consumes the seal -- that is a
    real look at the data, exactly what the one-shot guarantee is designed
    to capture, and this function has no say over it (it never runs again
    after Step 1's insert). This function's only job is to keep genuinely
    non-viable SETUP failures -- a malformed config, or a data_handler with
    no coverage of the registered holdout window -- from burning the seal
    before any evaluation was ever possible.

    Raises:
        HoldoutEvaluationPreflightError: ``config`` fails
            ``validate_backtest_config``, or ``data_handler`` reports fewer
            than ``_MIN_HOLDOUT_TRADING_SESSIONS`` trading dates in the
            effective ``[start_date, end_date]`` range.
    """
    try:
        validate_backtest_config(config)
    except Exception as exc:  # noqa: BLE001 -- any validate_backtest_config
        # failure is a config-viability SETUP problem, not a code bug; wrap
        # it uniformly as HoldoutEvaluationPreflightError so callers have one
        # exception type to catch for "this holdout attempt never got to run".
        raise HoldoutEvaluationPreflightError(
            "Holdout confirmation preflight failed: config is not a valid "
            f"backtest config ({type(exc).__name__}: {exc}). This is a SETUP "
            "failure caught before the one-shot holdout seal is consumed -- "
            "no holdout data was read. Fix the config and retry; the seal "
            "remains unconsumed."
        ) from exc

    bt_cfg = config["backtest"]
    start = _parse_date(bt_cfg["start_date"])
    end = _parse_date(bt_cfg["end_date"])
    sessions = data_handler.trading_dates(start, end)
    if len(sessions) < _MIN_HOLDOUT_TRADING_SESSIONS:
        raise HoldoutEvaluationPreflightError(
            f"Holdout confirmation preflight failed: only {len(sessions)} "
            f"trading session(s) available in the effective holdout window "
            f"({start} .. {end}); at least {_MIN_HOLDOUT_TRADING_SESSIONS} "
            "are required to run a single-window backtest and compute even "
            "one daily return. This is a SETUP failure -- no holdout "
            "returns were read -- caught before the one-shot holdout seal "
            "is consumed. Provide a data_handler that actually covers the "
            "registered holdout window and retry; the seal remains "
            "unconsumed."
        )


def _config_with_data_version(config: dict, data_version: str) -> dict:
    """Return a deep copy of ``config`` with its top-level ``data_version``
    set to the validated ``data_version`` argument (04-2 round-2 P2 fix).

    Never mutates the caller's ``config`` dict. ``BacktestEngine.run`` reads
    ``config.get("data_version", "")`` directly at the top level (see
    ``backtesting/config_contract.py``'s note on why a nested
    ``backtest.data_version`` is rejected rather than accepted as a synonym),
    so this is the key that must carry the recorded value for the dispatched
    backtest -- and every downstream ``BacktestResult``/``WalkForwardResult``
    -- to agree with the ``strategy_trials`` row.
    """
    import copy

    cfg = copy.deepcopy(config)
    cfg["data_version"] = data_version
    return cfg


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

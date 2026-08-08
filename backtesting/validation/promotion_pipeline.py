"""``PromotionPipeline`` -- the Gate 04 slice 04-4 orchestrator
(docs/plans/04-strategy-selection-protocol-design.md §4.4, §7 row 04-4).

Ties the existing, independently-tested statistical instruments in
``backtesting/validation/`` into ONE recorded promotion decision:

    TrialRecorder.run_walk_forward
      -> SurvivalFunnel.check
      -> TrialRecorder.run_parameter_sweep   (grid sourced from the linked
                                               strategy_hypotheses row --
                                               never invented ad hoc)
      -> bootstrap_stress
      -> deflated_sharpe_ratio (n_trials sourced from a strategy_trials
                                 COUNT, never hand-typed)
      -> benjamini_hochberg (per-strategy_family, informational)

This module does NOT modify any of the wrapped instruments' internals; it
only calls them in the order and with the inputs §4.4 specifies. It also
does NOT build the ``validated`` Strategy Registry status or the
``backtesting -> validated`` transition (04-5), and does NOT build the
end-to-end example script (04-6).

Baked-in decisions (operator-resolved, §8; NOT re-litigated here)
-------------------------------------------------------------------------
- **overall_passed gate** = all six survival-funnel gates PASS AND
  sensitivity verdict == 'robust' AND stress verdict == 'solid'. Deflated
  Sharpe (DSR) is recorded but never gates ``overall_passed`` (§8 Q3,
  resolved INFORMATIONAL ONLY -- no numeric DSR floor). FDR is recorded,
  not a hard gate (this module never fails/blocks on an FDR rejection
  flag).
- **n_trials** = SUM(configs_tested) over the strategy's
  ``run_type='parameter_sweep_variant'``, ``window='train_oos'``
  ``strategy_trials`` rows, PLUS 1 per ``run_type='walk_forward'``,
  ``window='train_oos'`` row (a sweep of N variants counts as N configs
  tried, not 1 -- §4.4's explicit "n_trials sweep-counting policy"
  decision). Counted over ALL statuses (running/completed/errored), not
  just completed ones -- this is Gap 1's whole point: a discarded/crashed
  trial still counts. Persisted verbatim as
  ``promotion_decisions.n_trials_used``.
- **Parameter grid** is sourced exclusively from the linked
  ``strategy_hypotheses.param_grid_json`` (frozen at the first trial that
  cites it -- see ``strategy_registry.hypothesis``/
  ``backtesting.validation.trial_recorder``). No ad hoc grid invented at
  promotion time is admissible. A missing hypothesis / missing / empty
  grid fails closed with :class:`MissingParameterGridError` before any
  instrument runs.
- **Residual-bug surfacing (§8 Q8, §9)**: BUG-066 (cross-sectional
  minimum-eligible-count gate), BUG-068 (Wikipedia constituent count
  drift), and BUG-071 (score-leg same-session cutoff edge case) are
  stamped inline into every ``evidence_json`` bundle so a reviewer of a
  ``promotion_decisions`` row always sees the caveats, per the resolved
  Q8.
- **Re-run staleness (§8 Q7, resolved)**: this implementation always
  re-runs the walk-forward leg fresh through ``TrialRecorder`` rather than
  attempting to reconstitute a prior recorded trial. The resolved policy
  ("reuse only if the identical manifest-hash data_version was already
  used") is a pure compute-cost optimization, not a correctness
  requirement -- and the current ``strategy_trials`` schema persists only
  summary ``metrics_json`` (aggregate OOS metrics), not the raw daily
  ``oos_returns`` series or per-fold detail the survival funnel's
  ``avg_is_sharpe``/trade-count adapters and ``bootstrap_stress`` need. A
  genuine reuse path would require extending that schema (04-1/04-2
  scope, not 04-4) or re-deriving those inputs from a re-run engine call
  anyway, at which point "reuse" saves nothing. Always-fresh is therefore
  the correct, honest choice within 04-4's scope; it satisfies the
  resolved policy's correctness bar (C7 data-version pinning already
  guarantees byte-identical inputs across re-runs) while deferring the
  actual compute-cost optimization to a later slice if it is ever needed.
- **holdout_mode is GATED/DEFERRED (not yet supported)**: as of
  ``docs/plans/04-identity-evaluation-context-design.md`` (operator
  decision, Option 1) and its 04-4W implementation, a promoted strategy's
  ``config_hash`` EXCLUDES its ``backtest.start_date``/``end_date`` --
  identity and evaluation window are separate, so the frozen winner CAN in
  principle be re-evaluated over the sealed holdout window without
  changing its hash. What is still missing is the seal-safe,
  holdout-appropriate evaluation machinery itself (a single-fixed-window
  evaluator distinct from the fold-based ``WalkForwardValidator``, and the
  preflight that keeps a setup failure from prematurely consuming the
  one-shot holdout seal) -- that work is deferred to slice 04-4H, which
  builds on top of 04-4W. Until then, ``run(..., holdout_mode=True)`` fails
  closed immediately with :class:`HoldoutConfirmationNotSupportedError`,
  before any instrument or DB work happens. The parameter is kept on the
  signature for callers/tests that reference it. The train/OOS
  (``holdout_mode=False``) path described below is unaffected and fully
  supported.
- **evaluation window is a required, explicit per-measurement input
  (04-4W)**: ``run()`` requires an ``eval_window: EvaluationWindow``
  argument. It is threaded to ``TrialRecorder.run_walk_forward``/
  ``run_parameter_sweep`` unchanged, which inject it into the dispatched
  config copy the same way ``data_version`` already is. The date fields on
  ``StrategyDefinition.config`` (``defn.config["backtest"]["start_date"]``/
  ``["end_date"]``) are never read for this purpose -- per
  docs/plans/04-identity-evaluation-context-design.md, ``config_hash``
  excludes them from identity precisely so the SAME frozen definition can be
  measured over different windows; reading the window back out of the
  stored definition would silently defeat that.
- **MLflow logging failure degrades gracefully**: when a ``backtest_logger``
  IS supplied (as opposed to the ``backtest_logger=None`` "skip MLflow
  entirely" path) but raises during ``_log_to_mlflow`` (e.g. a transient
  MLflow outage), the exception is caught, a ``promotion_mlflow_logging_failed``
  warning is logged, and the run continues with ``mlflow_run_id=None``. The
  ``promotion_decisions`` DB row -- written by ``_persist_decision`` -- is
  always still recorded with the real stage results in that case; MLflow is
  a secondary convenience log and ``evidence_json`` already contains the
  full DSR/FDR detail, so a MLflow outage must never discard an otherwise-
  complete promotion decision. A failure inside ``_persist_decision`` itself
  (the DB write) is NOT caught and still propagates -- the DB row is the
  source of truth.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

import structlog
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backtesting.validation.bootstrap_stress import (
    BootstrapStressResult,
    bootstrap_stress as _default_bootstrap_stress,
)
from backtesting.validation.overfitting_checks import (
    benjamini_hochberg as _default_benjamini_hochberg,
    deflated_sharpe_ratio as _default_deflated_sharpe_ratio,
)
from backtesting.validation.parameter_sensitivity import (
    ParameterSensitivityResult,
    ParameterSweeper,
)
from backtesting.validation.survival_funnel import (
    SurvivalFunnel,
    SurvivalFunnelResult,
    avg_is_sharpe_from_wf,
    oos_trade_count_from_wf,
)
from backtesting.validation.trial_recorder import (
    TrialRecorder,
    _normalize_metric,
    _sanitize_metrics,
)
from backtesting.validation.walk_forward import WalkForwardResult, WalkForwardValidator
from strategy_registry.evaluation_window import EvaluationWindow
from strategy_registry.hypothesis import HypothesisRegistry, HypothesisNotFoundError
from strategy_registry.models import Base
from strategy_registry.registry import StrategyNotFoundError, StrategyRegistry
from strategy_registry.selection_models import PromotionDecision

logger = structlog.get_logger(__name__)


# PR #50 Codex round-1 R1-B fix (P1, promotion-integrity/C8 precondition).
# The minimum number of DISTINCT, successfully completed (finite OOS
# Sharpe) parameter-sweep variants required for the sensitivity verdict to
# be trusted at all. Below this, ParameterSweeper's own curve_fit_flag
# computation has no real statistical power: positive_fraction can only
# take the values {0.0, 1.0} at n=1 and {0.0, 0.5, 1.0} at n=2, so a
# single-combination grid -- or a grid where all but one variant failed --
# trivially satisfies the default min_positive_fraction=0.5 threshold on
# ONE data point and skips the std-dispersion gate entirely (guarded by
# n_valid > 1 in parameter_sensitivity.py), yielding a "robust" verdict
# with zero evidentiary weight. Originally introduced in 04-4A (merged
# a5b7e1c) and found on PR #50 (04-4W); a curve-fit strategy could
# otherwise clear the survival funnel with real live-capital (C8)
# consequences, since Gate 04's promotion decision is the precondition for
# any paper-to-live discussion. 3 is the smallest count at which
# positive_fraction stops being a near-coin-flip statistic: n=3 is the
# first grid size fine enough to distinguish "most variants passed"
# (0.67) from "all variants passed" (1.0) rather than collapsing to a
# single pass/fail bit.
MIN_SENSITIVITY_SWEEP_VARIANTS: int = 3


# ── Acknowledged residual limitations (design doc §9) ──────────────────────────
# Stamped inline into every evidence_json bundle per §8 Q8's resolution
# (surface inline, not just a one-time acknowledgment in the design doc).
# Keep this in sync with bugs.md's BUG-066/068/071 entries; this module does
# not fix any of them -- it only surfaces that a promotion decision was made
# while they remain open.
RESIDUAL_BUG_ACKNOWLEDGEMENTS: dict[str, dict[str, str]] = {
    "BUG-066": {
        "status": "open",
        "description": (
            "Cross-sectional scoring has no minimum-eligible-count gate; "
            "full-window suppression increases silent cross-section "
            "shrinkage."
        ),
        "caveat": (
            "The survival funnel's min_trade_count gate provides partial, "
            "indirect protection (pervasively shrunken cross-sections tend "
            "to trade thinly), but this is not a substitute for BUG-066's "
            "fix. A promoted strategy's underlying alpha scores could still "
            "have been computed from a silently shrunken cross-section on "
            "some dates."
        ),
    },
    "BUG-068": {
        "status": "open",
        "description": (
            "Wikipedia constituent history has bounded count drift "
            "(left-censored inflation ~3% recent era; sparse pre-2000 "
            "changes; 3 ticker-collision exclusions)."
        ),
        "caveat": (
            "Any walk-forward fold or holdout window drawing on the "
            "affected universe/date ranges inherits this drift. Biases "
            "toward mild over-inclusion, not survivorship; not expected to "
            "invalidate a promotion decision on its own, but the "
            "underlying universe is not a licensed point-in-time feed yet."
        ),
    },
    "BUG-071": {
        "status": "open",
        "description": (
            "IC-validation cutoff-aware adjustment uses one run-boundary "
            "cutoff, not a literal per-score-date cutoff (score leg only; "
            "the realized-return leg's more severe version is already "
            "fixed)."
        ),
        "caveat": (
            "Affects the alpha scores a strategy's signal is built from, "
            "one narrow single-session edge case per affected "
            "ticker/action. Bounded in scope; not expected to change any "
            "promotion verdict materially, but inherited unchanged by this "
            "pipeline."
        ),
    },
}


# ── Exceptions ────────────────────────────────────────────────────────────────


class PromotionPipelineError(Exception):
    """Base class for PromotionPipeline-specific errors."""


class MissingParameterGridError(PromotionPipelineError):
    """Raised when no pre-registered, frozen-eligible hypothesis grid is
    available for the strategy being promoted.

    Per §4.4: "an ad hoc grid invented after seeing the result is not
    admissible" -- a promotion needs a pre-registered
    ``strategy_hypotheses.param_grid_json``. This is a fail-closed
    precondition checked BEFORE any instrument (walk-forward included)
    runs, so a promotion attempt without a grid leaves no partial
    trial/evidence trail.

    Also raised when the grid IS structurally present but at least one
    key's candidate list is unusable (a scalar, a string, a non-sequence,
    or an empty list/tuple) -- ``ParameterSweeper`` would discover zero
    combinations for such a key, but only AFTER the expensive walk-forward
    had already run and recorded partial trial evidence. Validating shape
    here, before any instrument runs, preserves the same fail-before-
    instruments guarantee for this case.
    """


class HoldoutConfirmationNotSupportedError(PromotionPipelineError):
    """Raised immediately when ``run(..., holdout_mode=True)`` is called.

    Holdout confirmation is DEFERRED: as of 04-4W, a promoted strategy's
    ``config_hash`` EXCLUDES its ``backtest.start_date``/``end_date`` (see
    ``docs/plans/04-identity-evaluation-context-design.md``, operator
    decision, Option 1) -- so the frozen winner CAN in principle be
    re-evaluated over the sealed holdout window without changing its hash.
    What remains missing is the seal-safe, holdout-appropriate evaluation
    machinery itself (a single-fixed-window evaluator, and the preflight
    that keeps a setup failure from prematurely consuming the one-shot
    holdout seal), deferred to slice 04-4H, which builds on top of 04-4W.
    This is gated fail-closed until that slice lands. The ``holdout_mode``
    parameter is kept on ``run()`` so existing callers/tests referencing it
    still import; it now always raises before any instrument or DB work
    happens.
    """


class NonCanonicalConfigHashError(PromotionPipelineError):
    """Raised when ``run(strategy_id, config_hash, ...)`` is called for a
    REGISTERED ``strategy_id`` (has a ``Strategy`` lifecycle row) whose
    requested ``config_hash`` does not equal that row's frozen
    ``canonical_config_hash``.

    A registered strategy has exactly one frozen/registered winning
    ``strategy_definitions`` row -- the one pinned at ``register()`` time
    as ``canonical_config_hash``. If the same ``strategy_id`` also has
    OTHER (non-canonical) ``strategy_definitions`` rows on file (e.g. from
    earlier sweep/trial exploration), a caller passing one of those other
    hashes must not be allowed to generate promotion evidence -- or, worse,
    consume the strategy-level one-shot holdout seal -- for a definition
    other than the frozen winner. ``verify_config_integrity`` alone does
    not catch this: it only re-checks the lifecycle row's OWN
    ``canonical_config_hash`` against its source YAML, never against the
    ``config_hash`` the caller actually requested. This check is fail-closed
    and runs BEFORE any instrument (walk-forward included) dispatches, so a
    mismatched request never touches the holdout seal or leaves partial
    trial evidence.
    """


# ── Result dataclass ────────────────────────────────────────────────────────────


@dataclass
class PromotionResult:
    """Aggregate output of one ``PromotionPipeline.run`` invocation.

    Mirrors the ``promotion_decisions`` row persisted alongside it (the
    ``promotion_decision_id`` field is that row's primary key).
    """

    strategy_id: str
    config_hash: str
    n_trials_used: int
    dsr_value: Optional[float]
    funnel_passed: bool
    funnel_result: SurvivalFunnelResult
    sensitivity_verdict: Optional[str]
    sensitivity_result: Optional[ParameterSensitivityResult]
    stress_verdict: Optional[str]
    stress_result: BootstrapStressResult
    overall_passed: bool
    mlflow_run_id: Optional[str]
    evidence_json: dict[str, Any]
    promotion_decision_id: Optional[int] = None
    wf_result: Optional[WalkForwardResult] = field(default=None, repr=False)


# ── PromotionPipeline ─────────────────────────────────────────────────────────


class PromotionPipeline:
    """Orchestrates funnel -> sensitivity -> stress -> DSR/FDR into one
    recorded promotion decision (§4.4).

    Follows the same DB-access pattern as ``TrialRecorder``/
    ``HypothesisRegistry``/``StrategyRegistry``: one ``create_engine`` per
    instance, a SQLite ``PRAGMA foreign_keys=ON`` connect-event when the
    URL is SQLite, short-lived ``Session`` blocks per DB interaction.

    Args:
        db_url: Shared DB URL for the Strategy Registry / hypothesis /
            trial-recorder / promotion_decisions tables.
        data_version: The manifest-hash-shaped C7 data_version this
            pipeline invocation runs against. One pipeline instance is
            scoped to one data snapshot; construct a new instance for a
            different snapshot.
        walk_forward_validator: Injected for testing; defaults to a real
            ``WalkForwardValidator()``.
        parameter_sweeper: Injected for testing; defaults to a real
            ``ParameterSweeper()``.
        survival_funnel: Injected for testing/threshold overrides; defaults
            to a real ``SurvivalFunnel()`` (default thresholds).
        backtest_logger: Optional ``BacktestLogger``-like object (must
            implement ``log_walk_forward_run`` and ``log_promotion_decision``).
            ``None`` (the default) skips the MLflow leg entirely -- the
            pipeline still runs and persists ``promotion_decisions``
            without an ``mlflow_run_id``.
        bootstrap_stress_fn: Injected for testing; defaults to
            ``backtesting.validation.bootstrap_stress.bootstrap_stress``.
        deflated_sharpe_fn: Injected for testing; defaults to
            ``backtesting.validation.overfitting_checks.deflated_sharpe_ratio``.
        benjamini_hochberg_fn: Injected for testing; defaults to
            ``backtesting.validation.overfitting_checks.benjamini_hochberg``.
        n_reshuffles: Passed through to ``bootstrap_stress``. Default 500.
        stress_seed: Optional reproducibility seed for ``bootstrap_stress``.
        sharpe_std: Passed through to ``deflated_sharpe_ratio``.
        risk_free_rate: Passed through to ``deflated_sharpe_ratio``.
        fdr_alpha: Desired false discovery rate for the per-family
            ``benjamini_hochberg`` correction. Default 0.05.
        walk_forward_kwargs: Extra kwargs forwarded to
            ``TrialRecorder.run_walk_forward`` (e.g. ``n_folds``).
        sweep_kwargs: Extra kwargs forwarded to
            ``TrialRecorder.run_parameter_sweep``.
        experiment_name: MLflow experiment name used for the walk-forward
            leg's ``log_walk_forward_run`` call.
        require_manifest_data_version: Forwarded to
            ``log_walk_forward_run``. Default True (a promotion decision
            is exactly the C7/03A-5 "real" call site the flag exists for).
    """

    def __init__(
        self,
        db_url: str,
        data_version: str,
        *,
        walk_forward_validator: Optional[WalkForwardValidator] = None,
        parameter_sweeper: Optional[ParameterSweeper] = None,
        survival_funnel: Optional[SurvivalFunnel] = None,
        backtest_logger: Optional[Any] = None,
        bootstrap_stress_fn: Callable[..., BootstrapStressResult] = _default_bootstrap_stress,
        deflated_sharpe_fn: Callable[..., float] = _default_deflated_sharpe_ratio,
        benjamini_hochberg_fn: Callable[..., list[bool]] = _default_benjamini_hochberg,
        n_reshuffles: int = 500,
        stress_seed: Optional[int] = None,
        sharpe_std: float = 1.0,
        risk_free_rate: float = 0.0,
        fdr_alpha: float = 0.05,
        walk_forward_kwargs: Optional[dict[str, Any]] = None,
        sweep_kwargs: Optional[dict[str, Any]] = None,
        experiment_name: str = "promotion_pipeline",
        require_manifest_data_version: bool = True,
    ) -> None:
        if not data_version or not data_version.strip():
            raise ValueError(
                "data_version is required to construct a PromotionPipeline (C7 "
                "discipline) -- pass the manifest-hash-shaped data_version this "
                "invocation runs against."
            )
        self._db_url = db_url
        self._data_version = data_version

        self._engine = create_engine(db_url, future=True)
        if db_url.startswith("sqlite"):
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        Base.metadata.create_all(self._engine)

        self._registry = StrategyRegistry(db_url)
        self._hypothesis_registry = HypothesisRegistry(db_url)
        self._trial_recorder = TrialRecorder(db_url)

        self._wf_validator = walk_forward_validator or WalkForwardValidator()
        self._sweeper = parameter_sweeper or ParameterSweeper()
        self._funnel = survival_funnel or SurvivalFunnel()
        self._backtest_logger = backtest_logger

        self._bootstrap_stress_fn = bootstrap_stress_fn
        self._deflated_sharpe_fn = deflated_sharpe_fn
        self._benjamini_hochberg_fn = benjamini_hochberg_fn

        self._n_reshuffles = n_reshuffles
        self._stress_seed = stress_seed
        self._sharpe_std = sharpe_std
        self._risk_free_rate = risk_free_rate
        self._fdr_alpha = fdr_alpha

        self._walk_forward_kwargs = walk_forward_kwargs or {}
        self._sweep_kwargs = sweep_kwargs or {}
        self._experiment_name = experiment_name
        self._require_manifest_data_version = require_manifest_data_version

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(
        self,
        strategy_id: str,
        config_hash: str,
        data_handler: Any,
        eval_window: EvaluationWindow,
        hypothesis_id: Optional[int] = None,
        holdout_mode: bool = False,
    ) -> PromotionResult:
        """Run the full promotion pipeline for one ``(strategy_id,
        config_hash)`` and persist a ``promotion_decisions`` row.

        Args:
            strategy_id: Matches an existing ``strategy_definitions`` row.
            config_hash: Matches an existing ``strategy_definitions`` row
                (composite key with ``strategy_id``).
            data_handler: Pre-loaded ``DataHandler`` covering the full
                date range, forwarded unchanged to the wrapped instruments.
            eval_window: The required, explicit evaluation date range for
                this promotion run (04-4W). Threaded unchanged to
                ``TrialRecorder.run_walk_forward``/``run_parameter_sweep``,
                which inject it into the dispatched config copy -- never
                read from ``StrategyDefinition.config``'s stored dates. See
                the module docstring's "evaluation window is a required,
                explicit per-measurement input" note.
            hypothesis_id: FK to the ``strategy_hypotheses`` row this
                promotion's parameter grid is sourced from. Required
                (never inferred) -- see ``MissingParameterGridError``.
            holdout_mode: DEFERRED/GATED -- kept on the signature for
                callers/tests that reference it, but passing ``True``
                always raises ``HoldoutConfirmationNotSupportedError``
                immediately (before any instrument or DB work). See the
                module docstring's "holdout_mode is GATED/DEFERRED" note
                and ``docs/plans/04-identity-evaluation-context-design.md``.

        Returns:
            A ``PromotionResult`` with every stage's outcome and the
            persisted ``promotion_decision_id``.

        Raises:
            HoldoutConfirmationNotSupportedError: ``holdout_mode=True`` was
                passed. Checked first, before any instrument or DB work.
            NonCanonicalConfigHashError: ``strategy_id`` is registered
                (has a ``Strategy`` lifecycle row) and ``config_hash`` does
                not equal that row's frozen ``canonical_config_hash``.
                Checked before any instrument or DB work.
            MissingParameterGridError: ``hypothesis_id`` is None, does not
                belong to ``strategy_id``, or its ``param_grid_json`` is
                missing/empty/malformed (a scalar, string, non-sequence, or
                empty list/tuple for any key). Checked before any
                instrument runs.
            DefinitionNotFoundError: No ``strategy_definitions`` row for
                ``(strategy_id, config_hash)`` (raised by
                ``TrialRecorder``/``StrategyRegistry``).
            ConfigDriftError: The strategy is already ``register()``-ed and
                its source YAML has drifted from the registered canonical
                hash (§4.3's config-freeze re-check).
            HoldoutWindowViolationError: The walk-forward date range
                violates the §4.2 train/OOS/holdout partition (propagated
                unchanged from ``TrialRecorder``).
        """
        if holdout_mode:
            raise HoldoutConfirmationNotSupportedError(
                "PromotionPipeline.run(holdout_mode=True) is not yet "
                "supported: as of 04-4W, config_hash EXCLUDES "
                "backtest.start_date/end_date, so the frozen winner's "
                "config CAN in principle be re-evaluated over the sealed "
                "holdout window without changing its hash -- but the "
                "seal-safe, holdout-appropriate evaluation machinery "
                "itself is not yet built. Holdout confirmation is deferred "
                "to slice 04-4H -- see "
                "docs/plans/04-identity-evaluation-context-design.md. Use "
                "holdout_mode=False (the train/OOS path) for now."
            )

        defn = self._registry.get_definition(strategy_id, config_hash)
        config = dict(defn.config)

        # §4.3 config-freeze re-check: if the strategy has already been
        # formally register()-ed, its source YAML must not have drifted
        # from the canonical hash pinned at registration. A strategy that
        # has never been register()-ed (only fingerprinted/add_definition-
        # ed) has no lifecycle row to drift-check against -- that is a
        # legitimate pre-registration promotion attempt, not an error.
        try:
            self._registry.verify_config_integrity(strategy_id)
        except StrategyNotFoundError:
            pass
        else:
            # verify_config_integrity only re-checks the lifecycle row's
            # OWN canonical_config_hash against its source YAML -- it never
            # compares that canonical hash against the config_hash THIS
            # call actually requested. A strategy_id can have multiple
            # strategy_definitions rows (e.g. earlier sweep/trial
            # exploration); without this check a caller could pass a
            # non-canonical config_hash for a REGISTERED strategy and
            # generate promotion evidence -- and, in holdout_mode, consume
            # the one-shot holdout seal -- for a definition other than the
            # frozen/registered winner. Bind the run to the canonical hash
            # here, before any instrument dispatches (see
            # NonCanonicalConfigHashError's docstring).
            registered_strategy = self._registry.get(strategy_id)
            if config_hash != registered_strategy.canonical_config_hash:
                raise NonCanonicalConfigHashError(
                    f"strategy_id={strategy_id!r} is registered with frozen "
                    f"canonical_config_hash="
                    f"{registered_strategy.canonical_config_hash!r}, but this "
                    f"run requested config_hash={config_hash!r}, which does "
                    "not match. A promotion/holdout run must target the "
                    "frozen registered winner -- refusing to generate "
                    "promotion evidence (or, in holdout_mode, consume the "
                    "one-shot holdout seal) for a non-canonical definition."
                )

        # Fail-closed precondition only (§4.4): confirms a valid,
        # non-empty grid exists BEFORE any instrument runs. The value read
        # here is deliberately NOT the one dispatched to the sweep below --
        # see the FIX 2 re-read comment at the sweep dispatch site for why.
        self._resolve_frozen_grid(strategy_id, hypothesis_id)
        strategy_family = self._resolve_strategy_family(strategy_id)

        # Stage 1: walk-forward (fresh; see module docstring's "re-run
        # staleness" note for why this never attempts to reuse a prior
        # recorded trial).
        wf_result = self._trial_recorder.run_walk_forward(
            self._wf_validator,
            strategy_id=strategy_id,
            config_hash=config_hash,
            data_version=self._data_version,
            config=config,
            eval_window=eval_window,
            data_handler=data_handler,
            hypothesis_id=hypothesis_id,
            final_holdout_confirmation=holdout_mode,
            strategy_family=strategy_family,
            **self._walk_forward_kwargs,
        )

        # Stage 2: survival funnel.
        funnel_result = self._funnel.check(
            oos_metrics=wf_result.oos_metrics,
            avg_is_sharpe=avg_is_sharpe_from_wf(wf_result),
            trade_count=oos_trade_count_from_wf(wf_result),
        )

        # Stage 3: parameter sensitivity -- skipped in holdout_mode
        # (TrialRecorder.run_parameter_sweep hard-rejects
        # final_holdout_confirmation=True; see module docstring).
        sensitivity_result: Optional[ParameterSensitivityResult] = None
        if not holdout_mode:
            # Re-read the hypothesis's param_grid_json from the DB here,
            # AFTER run_walk_forward above (which freezes the linked
            # hypothesis as a side effect of its first trial -- frozen_at
            # is now set). This closes a TOCTOU: the initial
            # _resolve_frozen_grid() read at the top of run() happens
            # BEFORE the freeze, so a concurrent
            # HypothesisRegistry.update_param_grid(...) committing between
            # that read and the freeze would leave the sweep dispatched
            # with the OLD grid while the now-frozen hypothesis row
            # records the NEW grid -- promotion evidence that wouldn't
            # match the frozen record it's supposed to attest to.
            # Re-fetching and re-validating the grid here guarantees the
            # sweep provably uses the immutable, already-frozen grid.
            frozen_param_grid = self._resolve_frozen_grid(strategy_id, hypothesis_id)
            sensitivity_result = self._trial_recorder.run_parameter_sweep(
                self._sweeper,
                strategy_id=strategy_id,
                config_hash=config_hash,
                data_version=self._data_version,
                base_config=config,
                eval_window=eval_window,
                param_grid=frozen_param_grid,
                data_handler=data_handler,
                hypothesis_id=hypothesis_id,
                strategy_family=strategy_family,
                **self._sweep_kwargs,
            )

        # Stage 4: bootstrap stress on the (possibly holdout) OOS returns.
        stress_result = self._bootstrap_stress_fn(
            wf_result.oos_returns,
            n_reshuffles=self._n_reshuffles,
            seed=self._stress_seed,
        )

        # Stage 5: n_trials (honest count from strategy_trials) + DSR.
        n_trials = self._compute_n_trials(strategy_id)
        observed_sharpe = wf_result.oos_metrics.get("sharpe")
        n_observations = int(len(wf_result.oos_returns.dropna()))
        dsr_value = self._compute_dsr(observed_sharpe, n_trials, n_observations)

        # Stage 6: per-family FDR (informational only -- never gates
        # overall_passed).
        fdr_evidence = self._compute_family_fdr(strategy_id, strategy_family, dsr_value)

        sensitivity_verdict = sensitivity_result.verdict if sensitivity_result else None
        stress_verdict = stress_result.verdict

        # PR #50 Codex round-1 R1-B fix (P1, promotion-integrity/C8): a
        # sweep's own curve_fit_flag computation degenerates with too few
        # DISTINCT, successfully completed (finite OOS Sharpe) variants --
        # a single valid variant with a positive Sharpe trivially satisfies
        # the default min_positive_fraction=0.5 threshold (positive_fraction
        # can only be 0.0 or 1.0 at n=1) and the std-dispersion gate is
        # skipped entirely (guarded by n_valid > 1 in ParameterSweeper.sweep),
        # so a single-combination grid -- or one where all but one variant
        # failed -- gets labelled "robust" with zero statistical power, and
        # overall_passed could become True with no meaningful parameter-
        # sensitivity test having run at all. Recompute the finite-variant
        # count HERE (not trusted from sensitivity_result.verdict alone) and
        # force the gate closed when it falls short of
        # MIN_SENSITIVITY_SWEEP_VARIANTS, regardless of what verdict the
        # sweep itself reported.
        #
        # PR #50 Codex round-2 fix (P1, promotion-integrity/C8): counting
        # raw finite rows is itself gameable -- _resolve_frozen_grid does
        # not reject duplicate values in a param_grid list (e.g.
        # {"portfolio.n_long": [10, 10, 10]}), so a grid with N copies of
        # the SAME combination produces N identical successful rows that
        # would satisfy MIN_SENSITIVITY_SWEEP_VARIANTS without testing any
        # actual parameter sensitivity. Dedupe finite rows by their
        # normalized params before counting distinct variants against the
        # threshold.
        #
        # PR #50 Codex round-3 fix (P2): a normalized key built from
        # tuple(sorted(row.params.items())) is unhashable the moment any
        # candidate value is itself a list/dict -- e.g.
        # {"universe": [{"source": "sp500"}, ...]} is a structurally-valid
        # grid entry per _resolve_frozen_grid's non-empty-list-or-tuple
        # check, and 04-3's strict-JSON param_grid validation
        # (json.dumps(..., allow_nan=False)) only guarantees candidates are
        # JSON-serializable, not scalar. Use a canonical JSON string
        # (sort_keys=True, so key order and nested dict-key order never
        # affect equality) as the dedup key instead -- json.dumps is total
        # over any JSON-serializable value, so this can never raise
        # TypeError the way hashing a tuple containing a dict/list would.
        n_finite_sensitivity_variants = (
            len(
                {
                    json.dumps(row.params, sort_keys=True)
                    for row in sensitivity_result.rows
                    if math.isfinite(row.oos_sharpe)
                }
            )
            if sensitivity_result is not None
            else None
        )
        sensitivity_underpowered = (
            n_finite_sensitivity_variants is not None
            and n_finite_sensitivity_variants < MIN_SENSITIVITY_SWEEP_VARIANTS
        )

        overall_passed = funnel_result.passed and stress_verdict == "solid"
        if not holdout_mode:
            overall_passed = (
                overall_passed
                and sensitivity_verdict == "robust"
                and not sensitivity_underpowered
            )

        evidence = self._build_evidence(
            strategy_id=strategy_id,
            config_hash=config_hash,
            holdout_mode=holdout_mode,
            funnel_result=funnel_result,
            sensitivity_result=sensitivity_result,
            n_finite_sensitivity_variants=n_finite_sensitivity_variants,
            sensitivity_underpowered=sensitivity_underpowered,
            stress_result=stress_result,
            dsr_value=dsr_value,
            n_trials=n_trials,
            n_observations=n_observations,
            observed_sharpe=observed_sharpe,
            fdr_evidence=fdr_evidence,
            overall_passed=overall_passed,
            eval_window=eval_window,
        )

        mlflow_run_id = self._log_to_mlflow(
            strategy_id=strategy_id,
            config_hash=config_hash,
            # Pass the DISPATCHED config (wf_result.config), not the
            # original StrategyDefinition.config. TrialRecorder dispatches
            # a copy with data_version injected (04-3), and WalkForwardResult
            # carries that copy as wf_result.config. log_walk_forward_run
            # compares the passed config's hash against wf_result's config;
            # passing the original here would always mismatch, raising
            # ConfigProvenanceMismatchError and getting silently swallowed
            # by the graceful-degradation catch below -- so MLflow logging
            # would never succeed in production.
            config=wf_result.config,
            wf_result=wf_result,
            funnel_result=funnel_result,
            stress_result=stress_result,
            dsr_value=dsr_value,
            n_trials=n_trials,
            n_observations=n_observations,
            fdr_evidence=fdr_evidence,
        )

        decision_id = self._persist_decision(
            strategy_id=strategy_id,
            config_hash=config_hash,
            n_trials_used=n_trials,
            dsr_value=dsr_value,
            funnel_passed=funnel_result.passed,
            sensitivity_verdict=sensitivity_verdict,
            stress_verdict=stress_verdict,
            overall_passed=overall_passed,
            mlflow_run_id=mlflow_run_id,
            evidence_json=evidence,
            # R1-A: sourced directly from the eval_window argument threaded
            # into this run() call -- never re-derived from
            # StrategyDefinition.config -- so promotion_decisions carries
            # the SAME window every other measurement sink now records.
            eval_start_date=eval_window.start,
            eval_end_date=eval_window.end,
        )

        logger.info(
            "promotion_pipeline_complete",
            strategy_id=strategy_id,
            config_hash=config_hash[:8],
            overall_passed=overall_passed,
            n_trials_used=n_trials,
            dsr_value=dsr_value,
            promotion_decision_id=decision_id,
        )

        return PromotionResult(
            strategy_id=strategy_id,
            config_hash=config_hash,
            n_trials_used=n_trials,
            dsr_value=dsr_value,
            funnel_passed=funnel_result.passed,
            funnel_result=funnel_result,
            sensitivity_verdict=sensitivity_verdict,
            sensitivity_result=sensitivity_result,
            stress_verdict=stress_verdict,
            stress_result=stress_result,
            overall_passed=overall_passed,
            mlflow_run_id=mlflow_run_id,
            evidence_json=evidence,
            promotion_decision_id=decision_id,
            wf_result=wf_result,
        )

    # ── Stage helpers ─────────────────────────────────────────────────────────

    def _resolve_frozen_grid(
        self, strategy_id: str, hypothesis_id: Optional[int]
    ) -> dict[str, list]:
        """Fail closed unless a pre-registered hypothesis with a non-empty
        ``param_grid_json`` is linked (§4.4: "an ad hoc grid invented after
        seeing the result is not admissible").
        """
        if hypothesis_id is None:
            raise MissingParameterGridError(
                f"PromotionPipeline.run requires a hypothesis_id for "
                f"strategy_id={strategy_id!r} so the parameter-sensitivity "
                "sweep uses a pre-declared, frozen grid rather than one "
                "invented at promotion time. Register a hypothesis with "
                "HypothesisRegistry.register_hypothesis(...) first."
            )
        try:
            hypothesis = self._hypothesis_registry.get_hypothesis(hypothesis_id)
        except HypothesisNotFoundError as exc:
            raise MissingParameterGridError(
                f"hypothesis_id={hypothesis_id} not found for "
                f"strategy_id={strategy_id!r}."
            ) from exc
        if hypothesis.strategy_id != strategy_id:
            raise MissingParameterGridError(
                f"hypothesis_id={hypothesis_id} belongs to strategy_id="
                f"{hypothesis.strategy_id!r}, not {strategy_id!r}. A "
                "promotion's parameter grid must be pre-registered for the "
                "exact strategy being promoted."
            )
        param_grid = hypothesis.param_grid_json
        if not param_grid:
            raise MissingParameterGridError(
                f"strategy_hypotheses id={hypothesis_id} (strategy_id="
                f"{strategy_id!r}) has no param_grid_json. A promotion "
                "needs a pre-registered grid; update it via "
                "HypothesisRegistry.update_param_grid(...) before recording "
                "any trial against this hypothesis (the grid freezes on "
                "the first linked trial)."
            )
        # A structurally-nonempty grid can still be unusable: a value that
        # is a scalar, a string, a non-sequence, or an empty list/tuple
        # means ParameterSweeper will discover ZERO combinations for that
        # key. Left unchecked, that failure only surfaces AFTER the
        # expensive walk-forward has already run and recorded partial
        # trial evidence -- violating the fail-before-instruments
        # precondition this method exists to enforce. Validate every
        # key's candidates are a non-empty list/tuple BEFORE returning.
        bad_keys = [
            key
            for key, candidates in param_grid.items()
            if not isinstance(candidates, (list, tuple)) or len(candidates) == 0
        ]
        if bad_keys:
            raise MissingParameterGridError(
                f"strategy_hypotheses id={hypothesis_id} (strategy_id="
                f"{strategy_id!r}) has a malformed param_grid_json: key(s) "
                f"{sorted(bad_keys)!r} must each be a non-empty list/tuple "
                "of candidate values (a scalar, a string, a non-sequence, "
                "or an empty list is not admissible -- it would let the "
                "expensive walk-forward run and record trial evidence "
                "before the sweep discovers zero usable combinations)."
            )
        return dict(param_grid)

    def _resolve_strategy_family(self, strategy_id: str) -> Optional[str]:
        try:
            strategy = self._registry.get(strategy_id)
        except StrategyNotFoundError:
            return None
        return strategy.strategy_family

    def _compute_n_trials(self, strategy_id: str) -> int:
        """§4.4's n_trials sweep-counting policy: SUM(configs_tested) over
        train_oos parameter_sweep_variant rows + 1 per train_oos
        walk_forward row. Counts every status (running/completed/errored)
        -- Gap 1's honest-count guarantee.
        """
        trials = self._trial_recorder.list_trials(strategy_id)
        n_trials = 0
        for trial in trials:
            if trial.window != "train_oos":
                continue
            if trial.run_type == "walk_forward":
                n_trials += 1
            elif trial.run_type == "parameter_sweep_variant":
                configs_tested = (trial.metrics_json or {}).get("configs_tested")
                try:
                    if configs_tested is None:
                        raise TypeError("configs_tested is None")
                    n_trials += int(configs_tested)
                except (TypeError, ValueError):
                    # Understates n_trials (weakens the DSR multiple-testing
                    # penalty) -- surface it so a reviewer can see a
                    # corrupted/legacy row silently fell back to counting 1.
                    logger.warning(
                        "promotion_n_trials_configs_tested_fallback",
                        trial_id=trial.id,
                        strategy_id=strategy_id,
                        raw_value=repr(configs_tested),
                    )
                    n_trials += 1
        return n_trials

    def _compute_dsr(
        self,
        observed_sharpe: Optional[float],
        n_trials: int,
        n_observations: int,
    ) -> Optional[float]:
        if observed_sharpe is None or not math.isfinite(float(observed_sharpe)):
            return None
        # n_trials < 2 is insufficient for a meaningful DSR: the default
        # expected-maximum-Sharpe term evaluates
        # norm.ppf(1 - 1/n_trials) = norm.ppf(0) = -inf when n_trials == 1,
        # which degenerates deflated_sharpe_ratio to return 1.0 for ANY
        # observed Sharpe (even negative) -- a false certainty. Treat
        # n_trials < 2 the same as an unavailable DSR (None) rather than
        # calling deflated_sharpe_ratio with a single trial.
        if n_trials < 2 or n_observations <= 1:
            logger.warning(
                "promotion_pipeline_dsr_skipped",
                reason="n_trials or n_observations insufficient",
                n_trials=n_trials,
                n_observations=n_observations,
            )
            return None
        raw_dsr = self._deflated_sharpe_fn(
            observed_sharpe=float(observed_sharpe),
            n_trials=n_trials,
            n_observations=n_observations,
            sharpe_std=self._sharpe_std,
            risk_free_rate=self._risk_free_rate,
        )
        # Normalize at the SOURCE: a non-finite (NaN/inf) return from
        # deflated_sharpe_fn must become None here, before this value feeds
        # _compute_family_fdr, evidence_json, the MLflow log call, and the
        # returned PromotionResult -- not just at the DB persistence sink.
        # Every downstream consumer must agree on the same normalized value.
        return _normalize_metric(raw_dsr)

    def _compute_family_fdr(
        self,
        strategy_id: str,
        strategy_family: Optional[str],
        dsr_value: Optional[float],
    ) -> dict[str, Any]:
        """Per-family Benjamini-Hochberg (§8 Q6, resolved PER-FAMILY):
        applied across sibling strategy_ids' latest promotion_decisions
        DSR values within the same strategy_family, using p ~= 1 - DSR as
        the per-strategy "probability of false discovery" proxy (DSR
        already estimates P(true SR > 0); no separate p-value is computed
        or stored anywhere upstream). Informational only -- never gates
        overall_passed. Skipped (with a clear reason) when this run's own
        dsr_value could not be computed.
        """
        if dsr_value is None:
            return {
                "skipped": True,
                "reason": "dsr_value unavailable for this run (insufficient n_trials/n_observations)",
            }

        sibling_ids = [strategy_id]
        if strategy_family is not None:
            with Session(self._engine) as session:
                from strategy_registry.models import Strategy

                sibling_ids = [
                    s.strategy_id
                    for s in session.scalars(
                        select(Strategy).where(Strategy.strategy_family == strategy_family)
                    )
                ]
                if strategy_id not in sibling_ids:
                    sibling_ids.append(strategy_id)

        with Session(self._engine) as session:
            prior_rows = list(
                session.scalars(
                    select(PromotionDecision)
                    .where(PromotionDecision.strategy_id.in_(sibling_ids))
                    .order_by(PromotionDecision.created_at.desc())
                )
            )

        # Most-recent decision per sibling strategy_id (excluding the
        # current strategy_id, whose CURRENT dsr_value -- not any prior
        # persisted row -- represents this run). ``prior_rows`` is ordered
        # newest-first, so a sibling is HANDLED the first time its row is
        # encountered here, regardless of whether that newest row has a
        # usable dsr_value. If the newest row's dsr_value is None/non-finite,
        # the sibling is omitted from the FDR set entirely -- we must never
        # fall through to an older, staler row for that sibling (that would
        # silently violate the documented "latest decision per sibling"
        # scope).
        latest_by_sibling: dict[str, float] = {}
        seen_siblings: set[str] = set()
        for row in prior_rows:
            if row.strategy_id == strategy_id:
                continue
            if row.strategy_id in seen_siblings:
                continue
            seen_siblings.add(row.strategy_id)
            if row.dsr_value is not None and math.isfinite(float(row.dsr_value)):
                latest_by_sibling[row.strategy_id] = float(row.dsr_value)

        compared_ids = [strategy_id] + list(latest_by_sibling.keys())
        dsr_values = [dsr_value] + list(latest_by_sibling.values())
        p_values = [max(0.0, min(1.0, 1.0 - v)) for v in dsr_values]

        rejected_flags = self._benjamini_hochberg_fn(p_values, fdr=self._fdr_alpha)

        return {
            "skipped": False,
            "scope": strategy_family if strategy_family is not None else strategy_id,
            "scope_type": "strategy_family" if strategy_family is not None else "strategy_id_only",
            "compared_strategy_ids": compared_ids,
            "p_values": p_values,
            "rejected_flags": rejected_flags,
            "current_strategy_rejected": rejected_flags[0] if rejected_flags else None,
            "fdr_alpha": self._fdr_alpha,
            "note": (
                "p-value proxy = 1 - dsr_value (no separate p-value is "
                "computed/stored upstream of DSR). Informational only; "
                "does not gate overall_passed."
            ),
        }

    def _build_evidence(
        self,
        *,
        strategy_id: str,
        config_hash: str,
        holdout_mode: bool,
        funnel_result: SurvivalFunnelResult,
        sensitivity_result: Optional[ParameterSensitivityResult],
        n_finite_sensitivity_variants: Optional[int],
        sensitivity_underpowered: bool,
        stress_result: BootstrapStressResult,
        dsr_value: Optional[float],
        n_trials: int,
        n_observations: int,
        observed_sharpe: Optional[float],
        fdr_evidence: dict[str, Any],
        overall_passed: bool,
        eval_window: EvaluationWindow,
    ) -> dict[str, Any]:
        return {
            "strategy_id": strategy_id,
            "config_hash": config_hash,
            "data_version": self._data_version,
            # R1-A: the same eval_window persisted on this row's
            # eval_start_date/eval_end_date columns (migration 018),
            # surfaced in evidence_json too for auditability -- mirrors
            # data_version already being both a first-class column AND an
            # evidence_json field.
            "eval_window": {
                "start": eval_window.start.isoformat(),
                "end": eval_window.end.isoformat(),
            },
            "holdout_mode": holdout_mode,
            "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
            "overall_passed": overall_passed,
            "funnel": {
                "passed": funnel_result.passed,
                "verdict": funnel_result.verdict,
                "gates": [
                    {
                        "name": g.name,
                        "passed": g.passed,
                        "value": g.value,
                        "threshold": g.threshold,
                        "description": g.description,
                    }
                    for g in funnel_result.gates
                ],
            },
            "sensitivity": (
                {
                    "skipped": True,
                    "reason": "holdout_mode confirmation runs a single fixed config only",
                }
                if sensitivity_result is None
                else {
                    "skipped": False,
                    "verdict": sensitivity_result.verdict,
                    "configs_tested": sensitivity_result.configs_tested,
                    "mean_oos_sharpe": sensitivity_result.mean_oos_sharpe,
                    "std_oos_sharpe": sensitivity_result.std_oos_sharpe,
                    "positive_fraction": sensitivity_result.positive_fraction,
                    "param_grid": sensitivity_result.param_grid,
                    # R1-B (PR #50 Codex round-1 P1) + round-2 P1 dedupe
                    # fix: the DISTINCT (by normalized params) finite-
                    # variant count actually used to gate overall_passed,
                    # and whether it fell short of
                    # MIN_SENSITIVITY_SWEEP_VARIANTS -- auditable proof the
                    # "robust" verdict (if any) reflects a real,
                    # sufficiently-powered sweep over genuinely distinct
                    # parameter combinations, not a single lucky/surviving
                    # variant or repeated copies of the same combination.
                    "n_finite_variants": n_finite_sensitivity_variants,
                    "underpowered": sensitivity_underpowered,
                    "min_required_variants": MIN_SENSITIVITY_SWEEP_VARIANTS,
                }
            ),
            "stress": {
                "verdict": stress_result.verdict,
                "n_reshuffles": stress_result.n_reshuffles,
                "drawdown_p5": stress_result.drawdown_p5,
                "drawdown_p50": stress_result.drawdown_p50,
                "drawdown_p95": stress_result.drawdown_p95,
                "worst_case_drawdown": stress_result.worst_case_drawdown,
            },
            "overfitting": {
                "observed_sharpe": observed_sharpe,
                "n_trials_used": n_trials,
                "n_observations": n_observations,
                "dsr_value": dsr_value,
                "dsr_informational_only": True,
                "fdr": fdr_evidence,
            },
            "residual_bug_acknowledgements": RESIDUAL_BUG_ACKNOWLEDGEMENTS,
        }

    def _log_to_mlflow(
        self,
        *,
        strategy_id: str,
        config_hash: str,
        config: dict,
        wf_result: WalkForwardResult,
        funnel_result: SurvivalFunnelResult,
        stress_result: BootstrapStressResult,
        dsr_value: Optional[float],
        n_trials: int,
        n_observations: int,
        fdr_evidence: dict[str, Any],
    ) -> Optional[str]:
        if self._backtest_logger is None:
            return None
        # MLflow is a SECONDARY convenience log; the promotion_decisions DB
        # row is the AUTHORITATIVE audit record (evidence_json already
        # carries the full DSR/FDR detail). A transient MLflow outage here
        # must degrade gracefully to mlflow_run_id=None rather than
        # discarding an otherwise-complete, expensive promotion decision.
        try:
            run_id = self._backtest_logger.log_walk_forward_run(
                config,
                wf_result,
                self._experiment_name,
                funnel_result=funnel_result,
                stress_result=stress_result,
                require_manifest_data_version=self._require_manifest_data_version,
            )
            self._backtest_logger.log_promotion_decision(
                run_id=run_id,
                dsr_value=dsr_value,
                n_trials=n_trials,
                n_observations=n_observations,
                fdr_rejected=fdr_evidence.get("current_strategy_rejected"),
                fdr_alpha=self._fdr_alpha,
            )
            return run_id
        except Exception as exc:  # noqa: BLE001 -- deliberate broad catch;
            # any MLflow failure mode (network, auth, schema) must not
            # discard a completed promotion decision.
            logger.warning(
                "promotion_mlflow_logging_failed",
                error=repr(exc),
                strategy_id=strategy_id,
                config_hash=config_hash[:8],
            )
            return None

    def _persist_decision(
        self,
        *,
        strategy_id: str,
        config_hash: str,
        n_trials_used: int,
        dsr_value: Optional[float],
        funnel_passed: bool,
        sensitivity_verdict: Optional[str],
        stress_verdict: Optional[str],
        overall_passed: bool,
        mlflow_run_id: Optional[str],
        evidence_json: dict[str, Any],
        eval_start_date: date,
        eval_end_date: date,
    ) -> int:
        now = datetime.now(tz=timezone.utc)
        with Session(self._engine) as session:
            decision = PromotionDecision(
                strategy_id=strategy_id,
                config_hash=config_hash,
                n_trials_used=n_trials_used,
                dsr_value=_normalize_metric(dsr_value),
                funnel_passed=funnel_passed,
                sensitivity_verdict=sensitivity_verdict,
                stress_verdict=stress_verdict,
                overall_passed=overall_passed,
                mlflow_run_id=mlflow_run_id,
                evidence_json=_sanitize_metrics(evidence_json),
                created_at=now,
                # R1-A: the EFFECTIVE evaluation window this decision's
                # walk-forward/sensitivity/stress legs actually ran over --
                # see migration 018's docstring for why this must never be
                # re-derived from StrategyDefinition.config.
                eval_start_date=eval_start_date,
                eval_end_date=eval_end_date,
            )
            session.add(decision)
            session.commit()
            session.refresh(decision)
            return decision.id

    # ── Read-only helpers ─────────────────────────────────────────────────────

    def list_promotion_decisions(self, strategy_id: str) -> list[PromotionDecision]:
        """Most-recent-first ``promotion_decisions`` rows for a strategy."""
        with Session(self._engine) as session:
            return list(
                session.scalars(
                    select(PromotionDecision)
                    .where(PromotionDecision.strategy_id == strategy_id)
                    .order_by(PromotionDecision.created_at.desc())
                )
            )

"""Fail-closed strategy-config contract for the backtesting path.

Roadmap 02B / BUG-075. Strategy config YAMLs (``config/strategy/*.yaml``)
declare a much richer surface than the backtest engine actually implements:
``portfolio.method`` can say ``mvo`` or ``risk_parity``, ``portfolio`` can
declare an ``optimizer_mode``, a whole ``constraints`` section can declare
``max_sector_weight``/``max_portfolio_beta``/``min_order_notional``, and a
``risk_model`` section can declare a covariance methodology -- but
``backtesting/engine/event_loop.py::BacktestEngine.run`` unconditionally runs
``_select_equal_weight`` regardless of what ``portfolio.method`` says, never
reads ``constraints`` or ``risk_model`` at all, and never reads
``portfolio.optimizer_mode``. Before this module existed, a backtest labeled
"mvo with a 25% sector cap and a 1.5 beta ceiling" was silently, indistin-
guishably an uncapped equal-weight backtest -- a research-validity lie
(the same defect class as BUG-009's methodology-honesty gap, just on the
strategy-config axis instead of the write-path axis).

This module is the ONE shared enforcement point (mirrors
``data.research.sql_compat.assert_methodology_write_is_honest``, added for
BUG-009 section 4) that every backtest-running call site must route through
via :func:`validate_backtest_config` *before* running anything. It does NOT
implement MVO/risk-parity/beta-constraint semantics -- that is out of scope
here and collides with the separate 03B loader work -- it only decides,
field by field, whether the backtest path's ACTUAL behavior can honestly
support what a config declares. Anything it cannot support is REJECTED
(:class:`UnsupportedStrategyConfigError`), never warned-and-continued.

Four-tier classification
--------------------------
Every field that can legally appear in a strategy config dict is one of:

``CONSUMED``
    The backtest path reads this field and its value materially changes
    what an individual backtest COMPUTES/SIMULATES (e.g.
    ``portfolio.n_long``, ``backtest.start_date``, ``name`` -- via
    ``backtesting/loader.py``'s alpha_scores strategy_id fallback, which
    changes WHICH SCORES get traded, not merely a label).

``CONSUMED_LOGGING_ONLY`` (PR #50 Codex round-8 fix, promotion-integrity/C8)
    The backtest path reads this field BY KEY, but only inside
    ``BacktestLogger`` (``log_run``/``log_walk_forward_run``) to label an
    MLflow record AFTER a run completes -- never inside
    ``BacktestEngine.run``/``WalkForwardValidator.run``/``ParameterSweeper.
    sweep``'s actual simulation, and never per-sweep-variant (only once,
    for the aggregate walk-forward leg). ``version`` (``strategy_version``
    MLflow tag) and ``reporting.save_positions``/``reporting.save_trades``
    (gate whether an artifact gets written, not what gets simulated) are
    the current members. This tier exists because ``PromotionPipeline``'s
    parameter-sensitivity dedupe (``backtesting/validation/
    promotion_pipeline.py``) needs to know "does varying this field change
    an individual SWEEP VARIANT's simulated output" -- a narrower question
    than plain ``CONSUMED``'s "is this field read anywhere in the backtest
    path, including post-hoc logging." Before this tier existed,
    ``CONSUMED`` conflated the two: a grid like ``{"version": [1, 2, 3]}``
    would count as 3 distinct behavioral variants even though
    ``ParameterSweeper.sweep``'s per-variant loop never invokes
    ``BacktestLogger`` at all, so all 3 backtest identically. See
    :func:`is_behavioral`.

``INFORMATIONAL``
    The backtest path does not read this field at all (not even for
    logging), but its presence does not misrepresent anything the backtest
    path computed. This covers two distinct cases: (a) pure metadata
    (``description``, ``created``) and (b) fields that describe a
    DIFFERENT subsystem's behavior honestly -- ``universe.*``/
    ``indicators.*`` describe how the upstream alpha_scores were computed
    (a separate, already-tracked research-run methodology, see BUG-009),
    and ``portfolio.target_volatility`` is a live/paper-only sizing knob
    (``portfolio/rebalancing``, ``portfolio/risk_model``) that the
    backtest engine has never claimed to apply. Declaring these alongside
    a backtest does not assert the backtest engine did anything with them.

Anything not explicitly classified CONSUMED, CONSUMED_LOGGING_ONLY, or
INFORMATIONAL below is REJECTED: an entire unknown top-level section, an
unknown key inside a known section, or a known field holding a value the
engine cannot honor (the canonical case: ``portfolio.method`` set to
anything other than ``"equal_weight"``, since ``_select_equal_weight`` is
the only construction method the engine actually runs).

What counts as "read" (adversarial-review sweep, 02B round 2; refined 04-4W
round-8 for the CONSUMED/CONSUMED_LOGGING_ONLY split)
-------------------------------------------------------------
Every classification below was audited against the actual keyed reads in
``backtesting/loader.py``, ``backtesting/engine/event_loop.py``,
``backtesting/experiment_tracking/mlflow_logger.py``,
``backtesting/validation/walk_forward.py``, and
``backtesting/validation/parameter_sensitivity.py``. A field is CONSUMED or
CONSUMED_LOGGING_ONLY iff some backtest-path code reads it BY KEY -- the
split is WHERE: inside the simulation itself (``BacktestEngine.run``/
``WalkForwardValidator.run``/``ParameterSweeper.sweep``/``loader.py``) is
CONSUMED; inside ``BacktestLogger`` ONLY is CONSUMED_LOGGING_ONLY. Bulk
verbatim recording does NOT count as either: ``_log_params_flat``, the
``config.json`` MLflow artifact, and ``config_hash`` copy the ENTIRE config
without interpreting any specific key, so "it appears in the params dump"
is true of every field and distinguishes nothing. INFORMATIONAL therefore
means: no keyed read anywhere in the backtest path (verified by grep for
each field), only possibly the verbatim bulk copies.

``execution.*`` is a special case: no backtest-path code reads
``config["execution"]`` to CONSTRUCT the ``FillSimulator`` (callers build
it directly and pass it in). Classifying it CONSUMED is made true not by a
construction-time read but by :func:`assert_fill_simulator_matches_config`,
which ``BacktestEngine.run`` calls to fail closed whenever a config
declares ``execution:`` parameters that differ from what the passed
simulator will actually apply. Relabeling it INFORMATIONAL instead was
explicitly rejected: declared cost parameters that may silently not match
the simulator is exactly the mislabeling defect this module exists to
kill.

Call sites (Roadmap 02B inventory, backtest path only -- the live/paper
execution path in ``execution/``/``portfolio/`` already implements MVO etc.
and is out of scope here):

* ``backtesting.engine.event_loop.BacktestEngine.run``
* ``backtesting.validation.walk_forward.WalkForwardValidator.run``
* ``backtesting.validation.parameter_sensitivity.ParameterSweeper.sweep``
* ``backtesting.experiment_tracking.mlflow_logger.BacktestLogger.log_run``

``backtesting.validation.bootstrap_stress.bootstrap_stress`` and
``backtesting.validation.survival_funnel.SurvivalFunnel`` take an
already-computed ``WalkForwardResult``/return series, not a raw strategy
config dict, so they are not separate entry points -- any config that
reached them already passed validation further upstream when the
``WalkForwardResult`` was produced.
"""

from __future__ import annotations

from typing import Any, Mapping

CONSUMED = "consumed"
CONSUMED_LOGGING_ONLY = "consumed_logging_only"
INFORMATIONAL = "informational"

# PR #50 Codex round-8 fix: statuses under which varying a field can NEVER
# change what an individual backtest/sweep-variant COMPUTES -- see
# is_behavioral() and the module docstring's four-tier classification.
_NON_BEHAVIORAL_STATUSES = frozenset({INFORMATIONAL, CONSUMED_LOGGING_ONLY})


def is_behavioral(dot_path: str) -> bool:
    """True iff varying ``dot_path``'s value could change what an
    individual backtest/sweep-variant actually SIMULATES -- i.e. its
    ``field_status()`` is neither ``INFORMATIONAL`` nor
    ``CONSUMED_LOGGING_ONLY``.

    The single source of truth for "is this field a meaningful parameter-
    sensitivity sweep dimension," used by ``PromotionPipeline`` (both the
    fail-closed grid-validation rejection and the dedupe-key filter) --
    see the module docstring's ``CONSUMED_LOGGING_ONLY`` entry for why
    plain ``CONSUMED`` alone is not precise enough for that purpose.
    """
    return field_status(dot_path) not in _NON_BEHAVIORAL_STATUSES


class UnsupportedStrategyConfigError(Exception):
    """Raised by :func:`validate_backtest_config` when a strategy config
    declares a field, section, or value the backtest path does not
    implement (Roadmap 02B / BUG-075).

    Deliberately NOT a subclass of ``ValueError``/``RuntimeError``: several
    backtest-running call sites (e.g.
    ``ParameterSweeper.sweep``'s per-variant loop) catch those broad
    exception types to record a data-availability failure as a single NaN
    variant and continue the sweep. A rejected config is not a data problem
    -- it is a request to run something the engine cannot honestly run --
    and must propagate and halt, never be swallowed into a "no warn, just
    continue with a worse result" path.
    """


class ConfigProvenanceMismatchError(UnsupportedStrategyConfigError):
    """Raised by ``BacktestLogger`` when the config it validated is not the
    config whose derivatives (``config_hash``, ``data_version``) it is
    about to persist (02B round-3 P2-2). A validated-looking MLflow record
    must never carry provenance computed from a different -- possibly
    unvalidated -- config object. Subclasses
    :class:`UnsupportedStrategyConfigError` for the same cannot-be-swallowed
    propagation semantics.
    """


class ExecutionConfigMismatchError(UnsupportedStrategyConfigError):
    """Raised by :func:`assert_fill_simulator_matches_config` when a config
    declares ``execution:`` cost parameters that differ from what the
    ``FillSimulator`` instance actually about to run will apply (02B
    round-2 P0-1). Subclasses :class:`UnsupportedStrategyConfigError` so it
    inherits the same cannot-be-swallowed propagation semantics.
    """


# ---------------------------------------------------------------------------
# Top-level scalar fields (no nested walk).
# ---------------------------------------------------------------------------
_TOP_LEVEL_FIELDS: dict[str, str] = {
    # CONSUMED (02B round-2 P0-2): backtesting/loader.py uses `name` as the
    # alpha_scores strategy_id filter fallback
    # (`config.get("strategy_id", config.get("name", "v1"))`), and
    # BacktestLogger reads it for the `strategy_name` MLflow tag. A wrong
    # `name` therefore changes WHICH SCORES the backtest trades, not just a
    # label. The fallback is kept (rather than requiring an explicit
    # `strategy_id` in every config) because v1_base_momentum.yaml must
    # keep working unchanged and its stored score rows are keyed by its
    # display name; the loader now fails closed when the resolved id
    # matches zero score rows, so a name/stored-id mismatch can no longer
    # silently produce a cash-only backtest. Prefer declaring an explicit
    # top-level `strategy_id` in new configs.
    "name": CONSUMED,
    # CONSUMED_LOGGING_ONLY (02B round-2 sweep; reclassified from CONSUMED,
    # PR #50 Codex round-8 fix): read by key ONLY in BacktestLogger for the
    # `strategy_version` MLflow tag -- a record-labeling read, never read
    # inside BacktestEngine.run/WalkForwardValidator.run/ParameterSweeper.
    # sweep's actual simulation, and BacktestLogger is never invoked
    # per-sweep-variant. A wrong version misattributes every logged run
    # (still worth failing on if declared-vs-actual ever diverges), but
    # sweeping over it produces zero behaviorally distinct variants.
    "version": CONSUMED_LOGGING_ONLY,
    "description": INFORMATIONAL,
    "created": INFORMATIONAL,
    # Top-level data_version: read directly by BacktestEngine.run
    # (`config.get("data_version", "")`) and enforced non-empty by
    # BacktestLogger.log_run (C7). Must live HERE, not under `backtest:` --
    # see _BACKTEST_FIELDS below for why a nested `backtest.data_version`
    # is rejected rather than silently accepted as a synonym.
    "data_version": CONSUMED,
    # Optional alpha_scores filter key read by backtesting.loader.load_from_snapshot
    # (`config.get("strategy_id", config.get("name", "v1"))`).
    "strategy_id": CONSUMED,
}

# Sections whose internal structure is not enumerated field-by-field because
# they describe a DIFFERENT subsystem's already-tracked methodology (the
# upstream signal/universe pipeline), not the backtest engine's own
# computation. Any nested structure is accepted.
# (`reporting` was originally wildcard-informational too; 02B round-3 P2-1
# moved it to a known enumerated section because BacktestLogger.log_run now
# genuinely consumes its keys -- see _REPORTING_FIELDS.)
_WILDCARD_INFORMATIONAL_SECTIONS = {"universe", "indicators"}

# ---------------------------------------------------------------------------
# `portfolio:` section.
# ---------------------------------------------------------------------------
_PORTFOLIO_FIELDS: dict[str, str] = {
    "method": CONSUMED,  # value-restricted -- see _validate_portfolio_method
    "n_long": CONSUMED,
    "rebalance_frequency": CONSUMED,
    "min_holding_days": CONSUMED,
    "max_position_weight": CONSUMED,
    # Live/paper-only vol-targeting sizing knob (portfolio/risk_model,
    # portfolio/rebalancing). BacktestEngine.run never reads it -- the
    # backtest always sizes positions via equal weight capped at
    # max_position_weight. Declaring it does not claim the backtest applied
    # vol targeting.
    "target_volatility": INFORMATIONAL,
}

# The only portfolio construction method the backtest engine actually runs.
# `_select_equal_weight` in backtesting/engine/event_loop.py is unconditional
# -- there is no branch on `portfolio.method` at all today, so any other
# declared value would be silently mislabeled equal-weight output.
_SUPPORTED_PORTFOLIO_METHOD = "equal_weight"

# ---------------------------------------------------------------------------
# `execution:` section -- maps 1:1 to FillSimulator's constructor kwargs
# (see backtesting/engine/fill_simulator.py). CONSUMED is made true by
# assert_fill_simulator_matches_config (called from BacktestEngine.run),
# NOT by any construction-time read: callers build the FillSimulator
# directly and pass it in, so without that assertion the declared values
# were unverifiable claims (02B round-2 P0-1). The dict values are the
# FillSimulator attribute names each declared key must match.
# ---------------------------------------------------------------------------
_EXECUTION_FIELDS: dict[str, str] = {
    "fill_model": CONSUMED,
    "bid_ask_spread_bps": CONSUMED,
    "market_impact_coeff": CONSUMED,
    "commission_per_share": CONSUMED,
}

# Declared `execution:` key -> FillSimulator introspection attribute whose
# actual value it must equal. Keys here must stay in lockstep with
# _EXECUTION_FIELDS' CONSUMED entries.
_EXECUTION_KEY_TO_SIM_ATTR: dict[str, str] = {
    "fill_model": "fill_model",
    "bid_ask_spread_bps": "bid_ask_spread_bps",
    "market_impact_coeff": "market_impact_coeff",
    "commission_per_share": "commission_per_share",
}

# Relative tolerance for numeric declared-vs-actual comparison. Tight
# enough that any real configuration difference fails; loose enough that a
# YAML float's repr round-trip cannot.
_EXECUTION_NUMERIC_RTOL = 1e-9

# ---------------------------------------------------------------------------
# `backtest:` section.
# ---------------------------------------------------------------------------
_BACKTEST_FIELDS: dict[str, str] = {
    "start_date": CONSUMED,
    "end_date": CONSUMED,
    "initial_capital": CONSUMED,
    # Documents which benchmark the pinned snapshot is expected to hold, but
    # nothing cross-checks it against the actual pinned benchmark snapshot
    # (see BUG-075 note in bugs.md) -- informational only, not consumed.
    "benchmark": INFORMATIONAL,
    # NOTE: `data_version` is deliberately absent here. BacktestEngine.run
    # only reads TOP-LEVEL `config["data_version"]`; a `backtest.data_version`
    # key (as v2_mvo_momentum.yaml currently declares) is never read by any
    # backtest-path code and would leave `BacktestResult.data_version` empty
    # -- silently defeating the C7 gate at MLflow-logging time instead of
    # failing here where the mistake is obvious. Rejected, not accepted.
}

# Entire sections the backtest path never reads at all. Any presence -- even
# an empty dict -- is rejected, because declaring the section at all implies
# the author expects it to do something.
_REJECTED_SECTIONS = {
    # Declares MVO/risk-parity constraint limits (max_sector_weight,
    # max_portfolio_beta, min_order_notional). Only execution/portfolio's
    # live/paper ComplianceEngine + PortfolioConstraints implement these;
    # the backtest engine has no constraint handler at all.
    "constraints",
    # Declares a covariance estimation methodology (Ledoit-Wolf, lookback
    # windows). Only used by portfolio/risk_model/ (live/paper MVO path);
    # the backtest engine never estimates a covariance matrix.
    "risk_model",
}

# ---------------------------------------------------------------------------
# `reporting:` section (02B round-3 P2-1). Both keys are genuinely consumed
# by BacktestLogger.log_run's artifact block: `save_trades` gates the
# trades.csv artifact (defaults to True when absent, preserving the
# previously-unconditional behavior) and `save_positions` gates a
# positions.csv artifact built from BacktestResult.positions (defaults to
# False when absent, preserving the previous no-positions-artifact
# behavior). Before this, the section was wildcard-informational while
# v1_base_momentum.yaml declared both keys and the logger ignored them --
# the same classification-vs-actual-reads defect class as round 2's P0s.
#
# CONSUMED_LOGGING_ONLY, not plain CONSUMED (PR #50 Codex round-8 fix):
# both keys are read ONLY by BacktestLogger's post-run artifact block --
# they gate whether a CSV gets WRITTEN, never anything
# BacktestEngine.run/WalkForwardValidator.run/ParameterSweeper.sweep
# actually simulates. BacktestLogger is not invoked per-sweep-variant, so
# sweeping over either key produces zero behaviorally distinct variants.
# ---------------------------------------------------------------------------
_REPORTING_FIELDS: dict[str, str] = {
    "save_positions": CONSUMED_LOGGING_ONLY,
    "save_trades": CONSUMED_LOGGING_ONLY,
}

_KNOWN_SECTION_VALIDATORS: dict[str, dict[str, str]] = {
    "portfolio": _PORTFOLIO_FIELDS,
    "execution": _EXECUTION_FIELDS,
    "backtest": _BACKTEST_FIELDS,
    "reporting": _REPORTING_FIELDS,
}


def validate_backtest_config(config: Mapping[str, Any]) -> None:
    """Fail-closed gate: raise unless every field in ``config`` is one this
    repo's backtest path can honestly support (Roadmap 02B / BUG-075).

    Call this BEFORE running anything, at every backtest-running entry
    point (see the module docstring for the full call-site inventory).
    Collects every violation before raising so a single call surfaces the
    whole set of unsupported fields, not just the first one found.

    Args:
        config: Strategy config dict, as loaded from a
            ``config/strategy/*.yaml`` file (optionally with per-run
            overrides such as walk-forward date-window substitution or
            parameter-sweep dot-path overrides already applied).

    Raises:
        UnsupportedStrategyConfigError: ``config`` declares one or more
            fields, sections, or values the backtest path does not
            implement. The message lists every offending dot-path and why.
    """
    violations: list[str] = []

    for key, value in config.items():
        if key in _TOP_LEVEL_FIELDS:
            continue
        if key in _WILDCARD_INFORMATIONAL_SECTIONS:
            continue
        if key in _REJECTED_SECTIONS:
            violations.append(
                f"'{key}' section is not implemented by the backtest path "
                "(BUG-075) -- present with value "
                f"{value!r}. Remove it, or if this config is meant for the "
                "live/paper execution path (which does implement it), do "
                "not pass it to a backtest entry point."
            )
            continue
        if key in _KNOWN_SECTION_VALIDATORS:
            violations.extend(_validate_section(key, value, _KNOWN_SECTION_VALIDATORS[key]))
            continue
        violations.append(
            f"Unknown top-level config key '{key}' -- not in the backtest "
            "path's consumed-field contract (backtesting/config_contract.py) "
            "and not recognized as informational metadata."
        )

    if violations:
        raise UnsupportedStrategyConfigError(
            "Strategy config declares field(s) the backtest path does not "
            "implement (Roadmap 02B / BUG-075, fail-closed per project "
            "policy -- see backtesting/config_contract.py):\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


def assert_fill_simulator_matches_config(
    config: Mapping[str, Any], fill_simulator: Any
) -> None:
    """Fail closed unless every ``execution:`` parameter a config declares
    matches what ``fill_simulator`` will actually apply (02B round-2 P0-1).

    ``BacktestEngine.run`` calls this right after
    :func:`validate_backtest_config`. Because callers construct the
    ``FillSimulator`` themselves and pass it in, nothing else guarantees
    the declared cost parameters describe the simulator actually used --
    without this check a config declaring ``fill_model: transaction_cost``
    with 10 bps of spread could silently run (and be MLflow-logged) against
    a zero-cost ``perfect`` simulator, or vice versa. Only the keys the
    config actually declares are checked; a config with no ``execution:``
    section declares nothing about costs and passes vacuously.

    Args:
        config: Strategy config dict (already contract-validated, so any
            ``execution`` section contains only known keys).
        fill_simulator: The simulator instance about to be used. Must
            expose the read-only introspection properties added to
            ``FillSimulator`` (``fill_model``, ``bid_ask_spread_bps``,
            ``market_impact_coeff``, ``commission_per_share``); an object
            that does not is rejected rather than trusted blind.

    Raises:
        ExecutionConfigMismatchError: a declared parameter differs from the
            simulator's actual value, or the simulator does not expose the
            attribute needed to verify a declared parameter.
    """
    execution_cfg = config.get("execution")
    if not execution_cfg:
        return

    mismatches: list[str] = []
    for key, value in execution_cfg.items():
        attr = _EXECUTION_KEY_TO_SIM_ATTR.get(key)
        if attr is None:
            # validate_backtest_config already rejects unknown execution
            # keys; reaching here means it was not called first. Fail
            # closed anyway rather than skipping silently.
            mismatches.append(
                f"execution.{key} is declared but has no known FillSimulator "
                "attribute mapping -- run validate_backtest_config first."
            )
            continue
        try:
            actual = getattr(fill_simulator, attr)
        except AttributeError:
            mismatches.append(
                f"execution.{key}={value!r} is declared but the supplied "
                f"fill simulator ({type(fill_simulator).__name__}) does not "
                f"expose a readable '{attr}' attribute, so the declaration "
                "cannot be verified. Unverifiable is treated as mismatched."
            )
            continue
        if not _execution_values_equal(value, actual):
            mismatches.append(
                f"execution.{key}: config declares {value!r} but the "
                f"supplied fill simulator will actually apply {actual!r}."
            )

    if mismatches:
        raise ExecutionConfigMismatchError(
            "Strategy config's declared execution parameters do not match "
            "the FillSimulator actually about to run (Roadmap 02B / BUG-075 "
            "P0-1, fail-closed). A backtest must never carry cost-model "
            "labels that differ from the costs actually simulated:\n"
            + "\n".join(f"  - {m}" for m in mismatches)
        )


def _execution_values_equal(declared: Any, actual: Any) -> bool:
    """Compare a declared execution value with the simulator's actual one.

    Numerics compare with a tight relative tolerance (YAML float repr
    round-trips must not fail; real config differences must); everything
    else compares by equality.
    """
    import math

    if isinstance(declared, bool) or isinstance(actual, bool):
        return declared == actual
    if isinstance(declared, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(
            float(declared), float(actual), rel_tol=_EXECUTION_NUMERIC_RTOL, abs_tol=1e-12
        )
    return bool(declared == actual)


def _validate_section(
    section_name: str, value: Any, allowed: dict[str, str]
) -> list[str]:
    violations: list[str] = []
    if not isinstance(value, Mapping):
        violations.append(
            f"'{section_name}' must be a mapping, got {type(value).__name__}."
        )
        return violations

    for sub_key, sub_value in value.items():
        dot_path = f"{section_name}.{sub_key}"
        if sub_key not in allowed:
            violations.append(
                f"Unknown key '{dot_path}' -- not in the backtest path's "
                "consumed-field contract (backtesting/config_contract.py)."
            )
            continue
        if section_name == "portfolio" and sub_key == "method":
            violations.extend(_validate_portfolio_method(sub_value))

    return violations


def _validate_portfolio_method(value: Any) -> list[str]:
    if value != _SUPPORTED_PORTFOLIO_METHOD:
        return [
            f"portfolio.method={value!r} is not implemented by the backtest "
            f"path -- BacktestEngine.run unconditionally runs "
            f"'{_SUPPORTED_PORTFOLIO_METHOD}' construction regardless of "
            "this field (BUG-075); a backtest labeled with any other method "
            "would silently be equal-weight. Only "
            f"{_SUPPORTED_PORTFOLIO_METHOD!r} is supported until MVO/"
            "risk-parity portfolio construction is implemented inside the "
            "backtest engine itself."
        ]
    return []


def field_status(dot_path: str) -> str:
    """Classify a single config dot-path without running full validation.

    Used by the conformance test (``backtesting/tests/test_config_contract.py``)
    to assert every key that appears in any ``config/strategy/*.yaml`` file is
    explicitly accounted for by this module -- never merely "not mentioned
    and therefore silently allowed". Only the first one or two path segments
    are inspected (mirroring what :func:`validate_backtest_config` itself
    inspects); deeper segments under a wildcard-informational section are
    not walked further since the whole subtree is informational regardless
    of depth.

    Returns one of:

    * ``"consumed"`` -- read and used to control what an individual
      backtest/sweep-variant SIMULATES.
    * ``"consumed_value_restricted"`` -- read, but only a specific value is
      supported (currently only ``portfolio.method``); the caller must still
      check the actual value via :func:`validate_backtest_config`.
    * ``"consumed_logging_only"`` -- read BY KEY, but only inside
      ``BacktestLogger`` to label an MLflow record after a run completes,
      never inside the simulation itself and never per-sweep-variant. See
      :func:`is_behavioral` for the "does this field matter for a
      parameter-sensitivity sweep" question this status exists to answer.
    * ``"informational"`` -- accepted, but not read by the backtest path
      at all (not even for logging).
    * ``"section"`` -- the bare name of a known enumerated section
      (``portfolio``, ``execution``, ``backtest``) rather than a field
      inside it. The section name itself carries no classification --
      consumed/informational status lives at the sub-key level -- so it
      gets this distinct status instead of being mislabeled
      "informational" (02B round-2 P2-3).
    * ``"rejected"`` -- explicitly unsupported; any presence fails closed.
    * ``"unknown"`` -- neither classified as consumed/informational nor as
      an explicitly rejected section/key. This is the status a genuinely
      NEW, never-reviewed top-level config key gets -- the case the
      conformance test exists to catch before it can ship silently ignored.
    """
    parts = dot_path.split(".")
    top = parts[0]

    if len(parts) == 1:
        if top in _TOP_LEVEL_FIELDS:
            return _TOP_LEVEL_FIELDS[top]
        if top in _WILDCARD_INFORMATIONAL_SECTIONS:
            return INFORMATIONAL
        if top in _REJECTED_SECTIONS:
            return "rejected"
        if top in _KNOWN_SECTION_VALIDATORS:
            # A known section itself (not one of its sub-keys) carries no
            # independent classification -- that lives at the sub-key level
            # -- so return the distinct "section" status rather than
            # mislabeling the bare name informational (P2-3).
            return "section"
        return "unknown"

    if top in _WILDCARD_INFORMATIONAL_SECTIONS:
        return INFORMATIONAL
    if top in _REJECTED_SECTIONS:
        return "rejected"
    if top in _KNOWN_SECTION_VALIDATORS:
        sub = parts[1]
        allowed = _KNOWN_SECTION_VALIDATORS[top]
        if sub not in allowed:
            return "rejected"
        if top == "portfolio" and sub == "method":
            return "consumed_value_restricted"
        return allowed[sub]

    return "unknown"


def consumed_contract_summary() -> dict[str, dict[str, str]]:
    """Return the full classification table, keyed by dot-path, for
    reporting/documentation purposes (e.g. printed by a CLI, or used to
    build the conformance test's expected coverage set). Not used by
    :func:`validate_backtest_config` itself.
    """
    summary: dict[str, dict[str, str]] = {}
    for key, status in _TOP_LEVEL_FIELDS.items():
        summary[key] = {"status": status}
    for section in _WILDCARD_INFORMATIONAL_SECTIONS:
        summary[f"{section}.*"] = {"status": INFORMATIONAL}
    for section in _REJECTED_SECTIONS:
        summary[f"{section}.*"] = {"status": "rejected"}
    for section, fields in _KNOWN_SECTION_VALIDATORS.items():
        for sub_key, status in fields.items():
            summary[f"{section}.{sub_key}"] = {"status": status}
    return summary

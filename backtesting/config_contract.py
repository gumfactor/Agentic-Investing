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

Three-tier classification
--------------------------
Every field that can legally appear in a strategy config dict is one of:

``CONSUMED``
    The backtest path reads this field and its value materially changes
    backtest behavior (e.g. ``portfolio.n_long``, ``backtest.start_date``).

``INFORMATIONAL``
    The backtest path does not read this field, but its presence does not
    misrepresent anything the backtest path computed. This covers two
    distinct cases: (a) pure metadata (``name``, ``version``, ``description``)
    and (b) fields that describe a DIFFERENT subsystem's behavior honestly --
    ``universe.*``/``indicators.*`` describe how the upstream alpha_scores
    were computed (a separate, already-tracked research-run methodology, see
    BUG-009), and ``portfolio.target_volatility`` is a live/paper-only sizing
    knob (``portfolio/rebalancing``, ``portfolio/risk_model``) that the
    backtest engine has never claimed to apply. Declaring these alongside a
    backtest does not assert the backtest engine did anything with them.

Anything not explicitly classified CONSUMED or INFORMATIONAL below is
REJECTED: an entire unknown top-level section, an unknown key inside a known
section, or a known field holding a value the engine cannot honor (the
canonical case: ``portfolio.method`` set to anything other than
``"equal_weight"``, since ``_select_equal_weight`` is the only construction
method the engine actually runs).

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
INFORMATIONAL = "informational"


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


# ---------------------------------------------------------------------------
# Top-level scalar fields (no nested walk).
# ---------------------------------------------------------------------------
_TOP_LEVEL_FIELDS: dict[str, str] = {
    "name": INFORMATIONAL,
    "version": INFORMATIONAL,
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
_WILDCARD_INFORMATIONAL_SECTIONS = {"universe", "indicators", "reporting"}

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
# (see backtesting/engine/fill_simulator.py and the documented wiring in
# .claude/skills/backtest.md's "Programmatic usage" section).
# ---------------------------------------------------------------------------
_EXECUTION_FIELDS: dict[str, str] = {
    "fill_model": CONSUMED,
    "bid_ask_spread_bps": CONSUMED,
    "market_impact_coeff": CONSUMED,
    "commission_per_share": CONSUMED,
}

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

_KNOWN_SECTION_VALIDATORS: dict[str, dict[str, str]] = {
    "portfolio": _PORTFOLIO_FIELDS,
    "execution": _EXECUTION_FIELDS,
    "backtest": _BACKTEST_FIELDS,
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

    * ``"consumed"`` -- read and used to control backtest behavior.
    * ``"consumed_value_restricted"`` -- read, but only a specific value is
      supported (currently only ``portfolio.method``); the caller must still
      check the actual value via :func:`validate_backtest_config`.
    * ``"informational"`` -- accepted, but not read by the backtest path.
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
            # independent meaning -- classification lives at the sub-key
            # level below.
            return INFORMATIONAL
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

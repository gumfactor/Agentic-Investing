"""Fail-closed strategy-config contract for PIT eligibility filters.

Roadmap 03A-4a (docs/plans/03a-immutable-research-data-design.md §1.3,
§1.5). Strategy config YAMLs (``config/strategy/*.yaml``) declare
eligibility-shaped filters under ``universe:`` -- either flat keys
(``v1_base_momentum.yaml``: ``universe.min_market_cap_usd``,
``universe.min_adv_usd``) or a nested ``universe.filters:`` block
(``v2_mvo_momentum.yaml``: ``min_market_cap_usd``, ``min_adv_usd``,
``min_price_usd``, ``allowed_security_types``). None of these were ever
enforced against real point-in-time data -- 01B §1.3 explicitly forbids
silently substituting today's market cap/ADV/price/security-type for a
historical value, so a strategy declaring one of these filters was, until
this module, either silently ignored (``backtesting/config_contract.py``
classifies the whole ``universe`` section wildcard-INFORMATIONAL for the
backtest path) or -- worse, if some future caller read it naively -- would
silently apply the CURRENT value everywhere, which is exactly BUG-037/01B's
substitution defect on a different axis.

This module is the fail-closed classification + parsing layer this gate
requires (mirrors ``backtesting/config_contract.py``'s pattern):

* :func:`eligibility_filter_status` classifies a single declared filter key
  as ``PIT_SUPPORTED`` (has a real ``data/universe/runtime.py`` eligibility
  attribute backing it), ``FAIL_CLOSED_UNSUPPORTED`` (named, no PIT source
  -- e.g. ``min_market_cap_usd``, per the binding operator decision that
  yfinance has no filing-dated shares-outstanding source), or
  ``UNCLASSIFIED`` (a key nobody has reviewed yet -- treated identically to
  unsupported for fail-closed purposes, but reported with a distinct status
  so the conformance test can catch a genuinely new, never-reviewed key
  before it ships).
* :func:`parse_universe_eligibility_filters` turns a strategy config's
  declared filters into ``data.universe.runtime.FilterSpec`` objects for
  :func:`data.universe.runtime.load_historical_universe_as_of`, raising
  :class:`UnsupportedEligibilityFilterError` -- never silently dropping or
  silently passing -- for anything not ``PIT_SUPPORTED``.

Bottom-up method (mandatory per this repo's recurring lesson): every
eligibility-shaped key below was enumerated FROM the two shipped
``config/strategy/*.yaml`` files, not invented from the design doc's list.
The design doc's §1.4 also names ``market_cap_usd`` as excluded from the
certified filter set (no dated shares-outstanding source) and 01B §1.3
names "halted, or bankruptcy state" as currently unsourced -- both are
listed explicitly below as ``FAIL_CLOSED_UNSUPPORTED`` synonyms/examples
even though no current YAML declares a halted/bankruptcy filter, so a
future config that does still gets a named rejection instead of falling
through to the generic ``UNCLASSIFIED`` bucket.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from data.universe.runtime import EligibilityFilterOp, FilterSpec

PIT_SUPPORTED = "pit_supported"
FAIL_CLOSED_UNSUPPORTED = "fail_closed_unsupported"
UNCLASSIFIED = "unclassified"


class UnsupportedEligibilityFilterError(Exception):
    """Raised by :func:`parse_universe_eligibility_filters` when a strategy
    config declares an eligibility filter this gate cannot honestly
    evaluate against point-in-time data (03A-4a, §1.3/§1.5).

    Deliberately NOT swallowed by any caller: a config asking for a filter
    with no PIT source must fail config load, not silently run without that
    filter (01B §1.3's "do not substitute today's value" rule extends here
    to "do not substitute no filter at all" -- both are silent
    misrepresentation of what was actually screened).
    """


# ---------------------------------------------------------------------------
# Structural keys under `universe:` that are not filters at all -- skipped
# during enumeration, never classified as a filter.
# ---------------------------------------------------------------------------
_NON_FILTER_UNIVERSE_KEYS = {"source", "filters", "eligibility"}

# ---------------------------------------------------------------------------
# Legacy flat/nested filter keys, as they actually appear today in
# config/strategy/*.yaml (bottom-up: enumerated from v1_base_momentum.yaml
# and v2_mvo_momentum.yaml, not invented). Maps each key to the PIT
# eligibility attribute + comparison operator it would evaluate to.
# ---------------------------------------------------------------------------
_LEGACY_FILTER_TO_ATTRIBUTE: dict[str, tuple[str, EligibilityFilterOp]] = {
    "min_adv_usd": ("adv_usd_20d", EligibilityFilterOp.GTE),
    "min_price_usd": ("price_usd", EligibilityFilterOp.GTE),
    "allowed_security_types": ("security_type", EligibilityFilterOp.IN),
}

# Explicitly named, fail-closed-unsupported filter keys (binding operator
# decision, 03A-4a task brief): market_cap_usd is DROPPED from the
# certified filter set because yfinance has no filing-dated
# (known_at-comparable) shares-outstanding source (design doc §1.4). Any
# config declaring a market-cap filter must fail config load, not silently
# pass every ticker. `halted_flag`/`bankruptcy_flag` are 01B §1.3's named
# examples of currently-unsourced filters -- no shipped YAML declares them
# yet, but a future one that does gets a named rejection here rather than
# falling into the generic UNCLASSIFIED bucket.
_FAIL_CLOSED_UNSUPPORTED_FILTERS: dict[str, str] = {
    "min_market_cap_usd": (
        "market_cap_usd has no PIT source: yfinance provides no filing-dated "
        "(known_at-comparable) shares-outstanding series, so market_cap_usd "
        "cannot be certified point-in-time (design doc §1.4). Remove this "
        "filter until a dated fundamentals source exists."
    ),
    "max_market_cap_usd": (
        "market_cap_usd has no PIT source (see min_market_cap_usd note)."
    ),
    "halted_flag": (
        "halted state has no historical PIT source (01B §1.3 names this "
        "explicitly as currently unsourced)."
    ),
    "bankruptcy_flag": (
        "bankruptcy state has no historical PIT source (01B §1.3 names this "
        "explicitly as currently unsourced)."
    ),
}

# PIT-supported eligibility attributes this gate can actually evaluate,
# backed by data/universe/runtime.py's PITEligibilityLookup (Phase A: schema
# + read API only; Phase B populates the underlying rows).
PIT_SUPPORTED_ATTRIBUTES = {"adv_usd_20d", "price_usd", "security_type"}


def eligibility_filter_status(filter_key: str) -> str:
    """Classify a single ``universe`` filter key.

    Used both by :func:`parse_universe_eligibility_filters` (to decide
    whether to build a ``FilterSpec`` or raise) and by the conformance test
    (to assert every filter key appearing in any shipped strategy YAML is
    explicitly accounted for -- never silently new-and-unreviewed).
    """
    if filter_key in _LEGACY_FILTER_TO_ATTRIBUTE:
        return PIT_SUPPORTED
    if filter_key in _FAIL_CLOSED_UNSUPPORTED_FILTERS:
        return FAIL_CLOSED_UNSUPPORTED
    return UNCLASSIFIED


def iter_universe_filter_keys(config: Mapping[str, Any]) -> set[str]:
    """Enumerate every eligibility-shaped filter key declared in a loaded
    strategy config dict, from BOTH shipped shapes:

    * flat, directly under ``universe:`` (``v1_base_momentum.yaml``'s style)
    * nested under ``universe.filters:`` (``v2_mvo_momentum.yaml``'s style)
    * the new explicit ``universe.eligibility:`` block (§1.3), once configs
      migrate to it

    Structural keys (``source``, ``filters``, ``eligibility`` themselves)
    are excluded -- they are not filters, they are containers/labels.
    """
    universe_cfg = config.get("universe")
    if not isinstance(universe_cfg, Mapping):
        return set()

    keys: set[str] = set()
    for key, value in universe_cfg.items():
        if key in _NON_FILTER_UNIVERSE_KEYS:
            continue
        keys.add(key)

    nested_filters = universe_cfg.get("filters")
    if isinstance(nested_filters, Mapping):
        keys.update(nested_filters.keys())

    eligibility_block = universe_cfg.get("eligibility")
    if isinstance(eligibility_block, Mapping):
        keys.update(eligibility_block.keys())

    return keys


def parse_universe_eligibility_filters(
    config: Mapping[str, Any],
) -> dict[str, FilterSpec]:
    """Parse ``config``'s declared eligibility filters into ``FilterSpec``
    objects, fail-closed (§1.3, §1.5).

    Reads, in order of precedence when present:

    1. ``universe.eligibility`` -- the new, explicit, structured block
       (§1.3): ``{attribute_name: {op: ..., threshold: ..., max_staleness_days: ...}}``.
       This is the preferred, forward-looking shape.
    2. ``universe.filters`` -- the legacy nested shape
       (``v2_mvo_momentum.yaml``).
    3. Flat keys directly under ``universe`` -- the legacy flat shape
       (``v1_base_momentum.yaml``).

    A config may combine shapes (e.g. legacy flat keys plus a new
    ``eligibility`` block); all declared filters across every shape present
    are parsed together. Every key not classified :data:`PIT_SUPPORTED` by
    :func:`eligibility_filter_status` raises
    :class:`UnsupportedEligibilityFilterError` naming every offending key at
    once (mirrors ``backtesting.config_contract.validate_backtest_config``'s
    "collect every violation before raising" behavior) -- never a partial,
    silently-narrowed filter set.

    Returns:
        ``{filter_key: FilterSpec}`` -- keyed by the ORIGINAL declared key
        (not the underlying attribute name), so a config declaring both
        ``min_price_usd`` and a hypothetical second price-shaped filter
        keeps them distinguishable by their source key.
    """
    universe_cfg = config.get("universe")
    if not isinstance(universe_cfg, Mapping):
        return {}

    violations: list[str] = []
    specs: dict[str, FilterSpec] = {}

    def _classify_and_build(key: str, raw_spec: Any) -> None:
        status = eligibility_filter_status(key)
        if status == FAIL_CLOSED_UNSUPPORTED:
            violations.append(
                f"universe filter '{key}' is not PIT-supported: "
                f"{_FAIL_CLOSED_UNSUPPORTED_FILTERS[key]}"
            )
            return
        if status == UNCLASSIFIED:
            violations.append(
                f"universe filter '{key}' is not classified by "
                "data/universe/eligibility_config.py (eligibility_filter_status "
                "returned 'unclassified'). A filter with no explicit "
                "PIT-supported-or-rejected classification must never silently "
                "pass every ticker (03A-4a, §1.3)."
            )
            return
        try:
            specs[key] = _build_filter_spec(key, raw_spec)
        except _FilterBuildError as exc:
            # A recognized (PIT-supported) key with a malformed value must
            # still fail-closed through this module's "collect every
            # violation before raising" contract, not crash with a raw
            # ValueError/TypeError that bypasses it (P2-1/P2-2).
            violations.append(str(exc))

    # 3. Flat legacy keys directly under `universe`.
    for key, value in universe_cfg.items():
        if key in _NON_FILTER_UNIVERSE_KEYS:
            continue
        _classify_and_build(key, value)

    # 2. Legacy nested `universe.filters`.
    nested_filters = universe_cfg.get("filters")
    if isinstance(nested_filters, Mapping):
        for key, value in nested_filters.items():
            _classify_and_build(key, value)

    # 1. New explicit `universe.eligibility` block -- structured spec dicts,
    # not legacy shorthand values, so build directly rather than via the
    # legacy attribute-name mapping.
    eligibility_block = universe_cfg.get("eligibility")
    if isinstance(eligibility_block, Mapping):
        for attribute_name, raw_spec in eligibility_block.items():
            if attribute_name not in PIT_SUPPORTED_ATTRIBUTES:
                violations.append(
                    f"universe.eligibility declares attribute "
                    f"'{attribute_name}', which is not one of the PIT-supported "
                    f"attributes {sorted(PIT_SUPPORTED_ATTRIBUTES)!r} (03A-4a)."
                )
                continue
            if not isinstance(raw_spec, Mapping) or "op" not in raw_spec or "threshold" not in raw_spec:
                violations.append(
                    f"universe.eligibility.{attribute_name} must be a mapping "
                    "with 'op' and 'threshold' keys."
                )
                continue
            try:
                op = EligibilityFilterOp(raw_spec["op"])
            except ValueError:
                violations.append(
                    f"universe.eligibility.{attribute_name}.op="
                    f"{raw_spec['op']!r} is not a recognized operator "
                    f"({[o.value for o in EligibilityFilterOp]!r})."
                )
                continue
            threshold = raw_spec["threshold"]
            if op is EligibilityFilterOp.IN:
                if not isinstance(threshold, (list, tuple)):
                    violations.append(
                        f"universe.eligibility.{attribute_name}: 'in' filters require "
                        "a list/tuple threshold."
                    )
                    continue
                coerced_threshold: Any = tuple(threshold)
            else:
                try:
                    coerced_threshold = float(threshold)
                except (TypeError, ValueError):
                    violations.append(
                        f"universe.eligibility.{attribute_name}.threshold="
                        f"{threshold!r} is not numeric for a {op.value} filter."
                    )
                    continue
            # max_staleness_days must be validated at parse time, not passed
            # through: an un-validated string/float here loads fine but
            # crashes with a raw TypeError at EVALUATION time in
            # PITEligibilityLookup.evaluate ((as_of_date - source_data_asof).days
            # > spec.max_staleness_days). Reject non-int (incl. bool/float)
            # and negatives with a named violation instead (fail-closed
            # honesty, same class as the threshold guard). bool is excluded
            # explicitly because it is an int subclass.
            max_staleness_days = raw_spec.get("max_staleness_days")
            if max_staleness_days is not None and (
                isinstance(max_staleness_days, bool)
                or not isinstance(max_staleness_days, int)
                or max_staleness_days < 0
            ):
                violations.append(
                    f"universe.eligibility.{attribute_name}.max_staleness_days="
                    f"{max_staleness_days!r} must be a non-negative integer or "
                    "omitted."
                )
                continue
            specs[f"eligibility.{attribute_name}"] = FilterSpec(
                attribute_name=attribute_name,
                op=op,
                threshold=coerced_threshold,
                max_staleness_days=raw_spec.get("max_staleness_days"),
            )

    if violations:
        raise UnsupportedEligibilityFilterError(
            "Strategy config declares eligibility filter(s) this gate cannot "
            "honestly evaluate against point-in-time data (Roadmap 03A-4a, "
            "fail-closed per §1.3/§1.5 -- see "
            "data/universe/eligibility_config.py):\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    return specs


class _FilterBuildError(Exception):
    """Internal: a recognized (PIT-supported) legacy filter key carried a
    malformed value. Caught in ``parse_universe_eligibility_filters`` and
    folded into the collected ``violations`` list so it surfaces as an
    ``UnsupportedEligibilityFilterError`` alongside every other problem,
    never as a raw ValueError/TypeError that bypasses the fail-closed
    contract (P2-1/P2-2)."""


def _build_filter_spec(key: str, raw_value: Any) -> FilterSpec:
    """Build a ``FilterSpec`` for a legacy filter key (flat or nested), whose
    shorthand value is just a threshold (numeric or a list of strings), not
    a structured ``{op, threshold}`` mapping.

    Raises :class:`_FilterBuildError` (never a raw ValueError/TypeError) when
    the value's shape does not match the operator: a non-numeric threshold on
    a numeric filter (P2-1), or a non-list/tuple/str on an ``IN`` filter
    (P2-2 -- e.g. a dict, which would otherwise parse into a filter that can
    never match anything and silently exclude EVERY ticker, masking a YAML
    typo as an illiquid universe)."""
    attribute_name, op = _LEGACY_FILTER_TO_ATTRIBUTE[key]
    if op is EligibilityFilterOp.IN:
        if isinstance(raw_value, (list, tuple)):
            threshold: Any = tuple(raw_value)
        elif isinstance(raw_value, str):
            threshold = (raw_value,)
        else:
            raise _FilterBuildError(
                f"universe filter '{key}' expects a list/tuple/str of allowed "
                f"values for an 'in' filter, got {type(raw_value).__name__} "
                f"({raw_value!r}). A non-collection value here would silently "
                "match no ticker and exclude the entire universe."
            )
    else:
        try:
            threshold = float(raw_value)
        except (TypeError, ValueError):
            raise _FilterBuildError(
                f"universe filter '{key}' expects a numeric threshold, got "
                f"{type(raw_value).__name__} ({raw_value!r})."
            )
    return FilterSpec(attribute_name=attribute_name, op=op, threshold=threshold)

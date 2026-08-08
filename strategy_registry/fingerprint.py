"""Canonical hashing, validation, and strategy ID normalisation for strategy configs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml

# Fingerprint algorithm version (04-4W). Bumped from the implicit v1 (dates
# included in config_hash) to v2 the moment _identity_view started excluding
# backtest.start_date/backtest.end_date from the hash input (see a000e87 /
# docs/plans/04-identity-evaluation-context-design.md, operator decision
# 2026-08-07, Option 1, back-compat explicitly waived -- nothing is live,
# C8 qualification has not started).
#
# Persisted verbatim on every new StrategyDefinition row
# (StrategyDefinition.fingerprint_algo_version, migration
# 015_fingerprint_algo_version.py) so the waiver is encoded in the schema,
# not just in a design doc: a pre-v2 row is now DISTINGUISHABLE from a v2
# row rather than silently assumed to have been hashed under the current
# algorithm. Existing (pre-migration) rows default to 1 (the old
# dates-included algorithm) via the migration's server_default; this module
# never rewrites any existing hash -- only a future, separate data migration
# would ever do that, and none is written here.
FINGERPRINT_ALGO_VERSION = 2

# Top-level keys stripped from the canonical hash input entirely. These are
# "evaluation context" (how/when a config was run), never "strategy
# identity" (what the strategy IS) -- see
# docs/plans/04-identity-evaluation-context-design.md.
_RUNTIME_KEYS: frozenset[str] = frozenset({"data_version"})

# Nested keys stripped from the canonical hash input, scoped to a specific
# parent section -- unlike _RUNTIME_KEYS these are NOT stripped from the
# canonical form used for storage/display (StrategyFingerprint.config), only
# from the input fed to _hash(). ``backtest.start_date``/``backtest.end_date``
# are the evaluation window: per docs/plans/04-identity-evaluation-context-
# design.md (operator decision, 2026-08-07, Option 1), the window is a
# per-measurement input, not part of a strategy's identity, so the SAME
# frozen config_hash must be reproducible whether evaluated over train/OOS
# dates or the sealed holdout window. The rest of "backtest" (e.g.
# initial_capital) remains part of identity and is NOT stripped here -- only
# these two leaf keys, nested under "backtest".
_IDENTITY_EXCLUDED_NESTED: dict[str, frozenset[str]] = {
    "backtest": frozenset({"start_date", "end_date"}),
}
_REQUIRED_TOP_LEVEL: frozenset[str] = frozenset(
    {"version", "name", "universe", "portfolio", "execution", "backtest"}
)
# Accept both the legacy "factors" key and the current "indicators" key.
# Configs registered before this rename used "factors"; their stored hashes
# must not change, so we read whichever key is present without normalising.
_INDICATORS_KEY_ALIASES: tuple[str, ...] = ("indicators", "factors")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,99}$")


@dataclass(frozen=True)
class StrategyFingerprint:
    strategy_id: str
    config_hash: str
    name: str
    version: int
    description: str | None
    portfolio_method: str | None
    n_long: int | None
    rebalance_frequency: str | None
    config: dict[str, Any]  # canonical form (runtime keys stripped, keys sorted;
    # backtest.start_date/end_date are RETAINED here -- they are excluded
    # only from config_hash's input, not from the stored/returned config)
    source_path: str
    # 04-4W: the FINGERPRINT_ALGO_VERSION this config_hash was computed
    # under. Always FINGERPRINT_ALGO_VERSION for a freshly computed
    # fingerprint -- see StrategyDefinition.fingerprint_algo_version
    # (migration 015) for why this is persisted rather than assumed.
    fingerprint_algo_version: int = FINGERPRINT_ALGO_VERSION


def fingerprint(
    config_path: str,
    explicit_strategy_id: str | None = None,
) -> StrategyFingerprint:
    """Load, validate, and canonically hash a strategy YAML. Does not write to DB."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Strategy config not found: {config_path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")

    validate_config(raw)

    strategy_id = resolve_strategy_id(raw, explicit_strategy_id)
    canonical = _canonical(raw)
    config_hash = _hash(_identity_view(canonical))

    portfolio = raw.get("portfolio") or {}

    return StrategyFingerprint(
        strategy_id=strategy_id,
        config_hash=config_hash,
        name=str(raw["name"]),
        version=int(raw["version"]),
        description=_optional_str(raw.get("description")),
        portfolio_method=_optional_str(portfolio.get("method")),
        n_long=int(portfolio["n_long"]) if portfolio.get("n_long") is not None else None,
        rebalance_frequency=_optional_str(portfolio.get("rebalance_frequency")),
        config=canonical,
        source_path=str(path.resolve()),
        fingerprint_algo_version=FINGERPRINT_ALGO_VERSION,
    )


def hash_config(config: Mapping[str, Any]) -> str:
    """Canonical ``config_hash`` for an already-in-memory config dict.

    Same canonicalisation as :func:`fingerprint`/:func:`recompute_hash` (key
    sorting + ``_RUNTIME_KEYS``/``_IDENTITY_EXCLUDED_NESTED`` stripping via
    :func:`_canonical`/:func:`_identity_view`, then :func:`_hash`) so a hash
    computed here is directly comparable to a
    ``StrategyDefinition.config_hash``/``StrategyFingerprint.config_hash``
    produced from the YAML file that originally registered it. Unlike
    :func:`fingerprint`, this does no file I/O and does not run
    :func:`validate_config` -- it exists for provenance-hash comparison of a
    config dict a caller already holds in memory (e.g.
    ``backtesting.validation.trial_recorder.TrialRecorder``'s C7 provenance
    check that the config passed to a wrapped ``validator.run``/
    ``sweeper.sweep`` call is the one that actually produced the claimed
    ``config_hash``), not for first-time registration.

    Per docs/plans/04-identity-evaluation-context-design.md (operator
    decision, 2026-08-07, Option 1), ``backtest.start_date``/
    ``backtest.end_date`` are evaluation context, not identity: two configs
    differing ONLY in those two nested keys hash IDENTICALLY. This is what
    lets a promotion pipeline evaluate the SAME frozen ``config_hash`` over
    train/OOS dates and then again over the sealed holdout window.
    """
    return _hash(_identity_view(_canonical(config)))


def recompute_hash(config_path: str) -> str:
    """Re-fingerprint a YAML on disk; used by verify_config_integrity (C6)."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Strategy config not found: {config_path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")
    return _hash(_identity_view(_canonical(raw)))


def validate_config(config: Mapping[str, Any]) -> None:
    """Raise ValueError on any structural problem before any DB write."""
    missing = _REQUIRED_TOP_LEVEL - set(config)
    if missing:
        raise ValueError(f"Strategy config missing required keys: {sorted(missing)}")

    indicators_key = next((k for k in _INDICATORS_KEY_ALIASES if k in config), None)
    if indicators_key is None:
        raise ValueError("strategy config must have an 'indicators' (or legacy 'factors') key")

    version = config["version"]
    if not isinstance(version, int) or version <= 0:
        raise ValueError("strategy config 'version' must be a positive integer")

    name = config["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("strategy config 'name' must be a non-empty string")

    indicators = config[indicators_key]
    if not isinstance(indicators, Mapping) or not indicators:
        raise ValueError("strategy config 'indicators' must be a non-empty mapping")
    total_weight = 0.0
    for indicator_name, indicator_cfg in indicators.items():
        if not isinstance(indicator_name, str) or not indicator_name.strip():
            raise ValueError("every indicator name must be a non-empty string")
        if not isinstance(indicator_cfg, Mapping):
            raise ValueError(f"indicator {indicator_name!r} must be a mapping")
        if "weight" not in indicator_cfg:
            raise ValueError(f"indicator {indicator_name!r} is missing required key 'weight'")
        total_weight += float(indicator_cfg["weight"])
    if total_weight <= 0:
        raise ValueError("strategy indicator weights must sum to a positive value")

    portfolio = config["portfolio"]
    if not isinstance(portfolio, Mapping):
        raise ValueError("strategy config 'portfolio' must be a mapping")
    n_long = portfolio.get("n_long", 0)
    if not isinstance(n_long, int) or isinstance(n_long, bool) or n_long <= 0:
        raise ValueError(
            "strategy config portfolio.n_long must be a positive integer "
            "(use 10, not 10.0 or '10')"
        )

    backtest = config["backtest"]
    if not isinstance(backtest, Mapping):
        raise ValueError("strategy config 'backtest' must be a mapping")
    for key in ("start_date", "end_date", "initial_capital", "benchmark"):
        if key not in backtest:
            raise ValueError(f"strategy config backtest.{key} is required")
    if date.fromisoformat(str(backtest["start_date"])) > date.fromisoformat(str(backtest["end_date"])):
        raise ValueError("strategy config backtest.start_date must be <= end_date")
    if float(backtest["initial_capital"]) <= 0:
        raise ValueError("strategy config backtest.initial_capital must be positive")


def resolve_strategy_id(
    config: Mapping[str, Any],
    explicit: str | None = None,
) -> str:
    """Derive and normalise a strategy_id. Falls back to v{version}_{name}."""
    raw = explicit or config.get("strategy_id") or f"v{config['version']}_{config['name']}"
    slug = _slug(str(raw))
    if not _ID_PATTERN.match(slug):
        raise ValueError(
            f"strategy_id {slug!r} must match ^[a-z][a-z0-9_]{{2,99}}$"
        )
    return slug


# ── internals ─────────────────────────────────────────────────────────────────


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _canonical(v)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if str(k) not in _RUNTIME_KEYS
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _identity_view(canonical: Any) -> Any:
    """Derive the hash-INPUT view from an already-canonical config: the same
    structure, minus the nested evaluation-context keys in
    ``_IDENTITY_EXCLUDED_NESTED`` (currently ``backtest.start_date``/
    ``backtest.end_date``).

    Deliberately separate from :func:`_canonical`, which is also used to
    build ``StrategyFingerprint.config``/the stored canonical config -- the
    date fields MUST remain in the stored/returned config (they are still
    validated by ``backtesting.config_contract`` and consumed by the
    backtest engine) and are only removed from what gets hashed. Never
    mutates ``canonical`` -- returns a new top-level dict (and a new nested
    dict for any affected section), copy-on-write for the rest.

    Missing sections/keys (no ``backtest`` section at all, or a ``backtest``
    section missing one or both date keys) are handled without error -- the
    dict comprehension below simply has nothing to exclude in that case.
    """
    if not isinstance(canonical, Mapping):
        return canonical
    result = dict(canonical)
    for section, excluded_keys in _IDENTITY_EXCLUDED_NESTED.items():
        nested = result.get(section)
        if isinstance(nested, Mapping):
            result[section] = {
                k: v for k, v in nested.items() if k not in excluded_keys
            }
    return result


def _hash(canonical: Any) -> str:
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _slug(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", lowered)
    return re.sub(r"_+", "_", slug).strip("_")

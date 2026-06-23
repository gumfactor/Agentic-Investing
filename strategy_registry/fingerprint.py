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

_RUNTIME_KEYS: frozenset[str] = frozenset({"data_version"})
_REQUIRED_TOP_LEVEL: frozenset[str] = frozenset(
    {"version", "name", "universe", "factors", "portfolio", "execution", "backtest"}
)
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
    config: dict[str, Any]  # canonical form (runtime keys stripped, keys sorted)
    source_path: str


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
    config_hash = _hash(canonical)

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
    )


def recompute_hash(config_path: str) -> str:
    """Re-fingerprint a YAML on disk; used by verify_config_integrity (C6)."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Strategy config not found: {config_path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")
    return _hash(_canonical(raw))


def validate_config(config: Mapping[str, Any]) -> None:
    """Raise ValueError on any structural problem before any DB write."""
    missing = _REQUIRED_TOP_LEVEL - set(config)
    if missing:
        raise ValueError(f"Strategy config missing required keys: {sorted(missing)}")

    version = config["version"]
    if not isinstance(version, int) or version <= 0:
        raise ValueError("strategy config 'version' must be a positive integer")

    name = config["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("strategy config 'name' must be a non-empty string")

    factors = config["factors"]
    if not isinstance(factors, Mapping) or not factors:
        raise ValueError("strategy config 'factors' must be a non-empty mapping")
    total_weight = 0.0
    for factor_name, factor_cfg in factors.items():
        if not isinstance(factor_name, str) or not factor_name.strip():
            raise ValueError("every factor name must be a non-empty string")
        if not isinstance(factor_cfg, Mapping):
            raise ValueError(f"factor {factor_name!r} must be a mapping")
        if "weight" not in factor_cfg:
            raise ValueError(f"factor {factor_name!r} is missing required key 'weight'")
        total_weight += float(factor_cfg["weight"])
    if total_weight <= 0:
        raise ValueError("strategy factor weights must sum to a positive value")

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

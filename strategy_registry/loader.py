from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class StrategyConfig:
    strategy_id: str
    config_path: str
    config_sha256: str
    version: int
    name: str
    description: str | None
    portfolio_method: str | None
    n_long: int | None
    rebalance_frequency: str | None


def load_and_fingerprint(strategy_id: str, config_path: str) -> StrategyConfig:
    """Load a strategy YAML and compute its SHA-256 fingerprint."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Strategy config not found: {config_path}")

    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    data = yaml.safe_load(raw)

    version = data.get("version") or data.get("version", 1)
    name = data.get("name") or strategy_id
    description = data.get("description")

    portfolio = data.get("portfolio") or {}
    portfolio_method = portfolio.get("method")
    n_long = portfolio.get("n_long")
    rebalance_frequency = portfolio.get("rebalance_frequency")

    return StrategyConfig(
        strategy_id=strategy_id,
        config_path=config_path,
        config_sha256=sha256,
        version=int(version),
        name=str(name),
        description=str(description) if description else None,
        portfolio_method=str(portfolio_method) if portfolio_method else None,
        n_long=int(n_long) if n_long is not None else None,
        rebalance_frequency=str(rebalance_frequency) if rebalance_frequency else None,
    )


def compute_sha256(config_path: str) -> str:
    return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()

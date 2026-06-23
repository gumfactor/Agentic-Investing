"""Unit tests for StrategyRegistry — uses SQLite in-memory DB; no external services."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from strategy_registry.registry import (
    ConfigDriftError,
    ConflictingActiveStrategyError,
    InvalidTransitionError,
    MissingDataVersionError,
    MissingOperatorNotesError,
    PerformanceSnapshot,
    StrategyAlreadyRegisteredError,
    StrategyNotFoundError,
    StrategyRegistry,
    StrategyStatus,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Write a minimal strategy YAML and return its path."""
    config = {
        "version": 1,
        "name": "test_strategy",
        "description": "A test strategy",
        "portfolio": {
            "method": "equal_weight",
            "n_long": 10,
            "rebalance_frequency": "monthly",
        },
    }
    p = tmp_path / "v1_test_strategy.yaml"
    p.write_text(yaml.dump(config))
    return p


@pytest.fixture
def registry(tmp_path: Path) -> StrategyRegistry:
    db_path = tmp_path / "test.db"
    return StrategyRegistry(f"sqlite:///{db_path}")


# ── Registration ──────────────────────────────────────────────────────────────


def test_register_creates_strategy(registry: StrategyRegistry, tmp_config: Path) -> None:
    s = registry.register("v1_test", str(tmp_config))
    assert s.strategy_id == "v1_test"
    assert s.status == StrategyStatus.BACKTESTING
    assert s.version == 1
    assert s.name == "test_strategy"
    assert s.config_sha256 == hashlib.sha256(tmp_config.read_bytes()).hexdigest()


def test_register_duplicate_raises(registry: StrategyRegistry, tmp_config: Path) -> None:
    registry.register("v1_test", str(tmp_config))
    with pytest.raises(StrategyAlreadyRegisteredError):
        registry.register("v1_test", str(tmp_config))


def test_register_archived_still_raises(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    # strategy_id is permanent — even archived IDs cannot be re-registered.
    # Create v2_... instead.
    registry.register("v1_test", str(tmp_config))
    registry.transition("v1_test", StrategyStatus.ARCHIVED)
    with pytest.raises(StrategyAlreadyRegisteredError):
        registry.register("v1_test", str(tmp_config))


# ── Transitions ───────────────────────────────────────────────────────────────


def test_transition_backtesting_to_paper(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    s = registry.transition("v1_test", StrategyStatus.PAPER)
    assert s.status == StrategyStatus.PAPER
    assert s.activated_paper_at is not None


def test_transition_paper_to_live_requires_notes(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    registry.transition("v1_test", StrategyStatus.PAPER)
    with pytest.raises(MissingOperatorNotesError):
        registry.transition("v1_test", StrategyStatus.LIVE)


def test_transition_paper_to_live_with_notes(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    registry.transition("v1_test", StrategyStatus.PAPER)
    s = registry.transition(
        "v1_test", StrategyStatus.LIVE, operator_notes="C8 clearance granted"
    )
    assert s.status == StrategyStatus.LIVE
    assert s.activated_live_at is not None


def test_transition_archived_is_terminal(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    registry.transition("v1_test", StrategyStatus.ARCHIVED)
    with pytest.raises(InvalidTransitionError):
        registry.transition("v1_test", StrategyStatus.BACKTESTING)


def test_transition_invalid_raises(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    with pytest.raises(InvalidTransitionError):
        # backtesting → live is not a valid direct transition
        registry.transition("v1_test", StrategyStatus.LIVE, operator_notes="skip paper")


def test_transition_not_found_raises(registry: StrategyRegistry) -> None:
    with pytest.raises(StrategyNotFoundError):
        registry.transition("nonexistent", StrategyStatus.PAPER)


def test_transition_conflict_paper(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    # Two strategies; first moves to paper; second should be blocked.
    cfg1 = tmp_path / "v1.yaml"
    cfg2 = tmp_path / "v2.yaml"
    cfg1.write_text(yaml.dump({"version": 1, "name": "s1", "portfolio": {"method": "equal_weight", "n_long": 10}}))
    cfg2.write_text(yaml.dump({"version": 2, "name": "s2", "portfolio": {"method": "equal_weight", "n_long": 10}}))

    registry.register("v1_s1", str(cfg1))
    registry.register("v2_s2", str(cfg2))
    registry.transition("v1_s1", StrategyStatus.PAPER)

    with pytest.raises(ConflictingActiveStrategyError):
        registry.transition("v2_s2", StrategyStatus.PAPER)


# ── Config integrity ──────────────────────────────────────────────────────────


def test_verify_config_integrity_passes(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    assert registry.verify_config_integrity("v1_test") is True


def test_verify_config_integrity_drift_raises(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    # Silently mutate the file after registration (simulates C6 violation)
    tmp_config.write_text(yaml.dump({"version": 1, "name": "modified_strategy"}))
    with pytest.raises(ConfigDriftError):
        registry.verify_config_integrity("v1_test")


# ── Performance snapshots ─────────────────────────────────────────────────────


def test_record_performance_backtest(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    snap = PerformanceSnapshot(
        snapshot_date=date(2024, 12, 31),
        period_type="backtest",
        period_start=date(2022, 7, 11),
        period_end=date(2024, 12, 31),
        sharpe_ratio=Decimal("0.82"),
        annualized_return=Decimal("0.14"),
        max_drawdown=Decimal("-0.09"),
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        mlflow_run_id="abc123",
    )
    row = registry.record_performance("v1_test", snap)
    assert row.strategy_id == "v1_test"
    assert row.sharpe_ratio == Decimal("0.82")


def test_record_performance_backtest_requires_data_version(
    registry: StrategyRegistry, tmp_config: Path
) -> None:
    registry.register("v1_test", str(tmp_config))
    snap = PerformanceSnapshot(
        snapshot_date=date(2024, 12, 31),
        period_type="backtest",
        data_version=None,  # missing — should fail
    )
    with pytest.raises(MissingDataVersionError):
        registry.record_performance("v1_test", snap)


# ── List and get ──────────────────────────────────────────────────────────────


def test_list_all(registry: StrategyRegistry, tmp_path: Path) -> None:
    for i in range(3):
        p = tmp_path / f"v{i+1}.yaml"
        p.write_text(yaml.dump({"version": i + 1, "name": f"s{i+1}", "portfolio": {"method": "equal_weight", "n_long": 10}}))
        registry.register(f"v{i+1}_s{i+1}", str(p))
    assert len(registry.list()) == 3


def test_list_filter_by_status(registry: StrategyRegistry, tmp_path: Path) -> None:
    p1 = tmp_path / "v1.yaml"
    p2 = tmp_path / "v2.yaml"
    p1.write_text(yaml.dump({"version": 1, "name": "s1", "portfolio": {"method": "equal_weight", "n_long": 10}}))
    p2.write_text(yaml.dump({"version": 2, "name": "s2", "portfolio": {"method": "equal_weight", "n_long": 10}}))
    registry.register("v1_s1", str(p1))
    registry.register("v2_s2", str(p2))
    registry.transition("v1_s1", StrategyStatus.PAPER)

    paper = registry.list(status=StrategyStatus.PAPER)
    assert len(paper) == 1
    assert paper[0].strategy_id == "v1_s1"


def test_get_not_found_raises(registry: StrategyRegistry) -> None:
    with pytest.raises(StrategyNotFoundError):
        registry.get("ghost")

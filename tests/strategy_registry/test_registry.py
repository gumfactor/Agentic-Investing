"""
Unit tests for StrategyRegistry — uses SQLite in-memory; no external services.
Covers the definition layer, lifecycle layer, and run recording layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from strategy_registry.fingerprint import fingerprint, recompute_hash, validate_config
from strategy_registry.registry import (
    ConfigDriftError,
    ConflictingActiveStrategyError,
    DefinitionNotFoundError,
    DuplicateVersionError,
    InvalidTransitionError,
    MissingDataVersionError,
    MissingOperatorNotesError,
    StrategyAlreadyRegisteredError,
    StrategyNotFoundError,
    StrategyRegistry,
    StrategyStatus,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _write_config(path: Path, *, version: int = 1, name: str = "test_strategy", weight: float = 1.0) -> Path:
    """Write a valid, complete strategy YAML."""
    config = {
        "version": version,
        "name": name,
        "description": f"Test strategy v{version}",
        "universe": {"source": "sp500"},
        "factors": {"momentum": {"weight": weight, "score_col": "momentum_score"}},
        "portfolio": {"method": "equal_weight", "n_long": 10, "max_position_weight": 0.1},
        "execution": {"fill_model": "perfect"},
        "backtest": {
            "start_date": "2022-01-01",
            "end_date": "2024-12-31",
            "initial_capital": 1000000.0,
            "benchmark": "SPY",
        },
    }
    path.write_text(yaml.dump(config), encoding="utf-8")
    return path


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return _write_config(tmp_path / "v1_test_strategy.yaml")


@pytest.fixture
def registry(tmp_path: Path) -> StrategyRegistry:
    return StrategyRegistry(f"sqlite:///{tmp_path / 'test.db'}")


# ── fingerprint module ────────────────────────────────────────────────────────


def test_fingerprint_produces_stable_hash(cfg: Path) -> None:
    fp1 = fingerprint(str(cfg))
    fp2 = fingerprint(str(cfg))
    assert fp1.config_hash == fp2.config_hash


def test_fingerprint_excludes_data_version(tmp_path: Path) -> None:
    p1, p2 = tmp_path / "a.yaml", tmp_path / "b.yaml"
    _write_config(p1, version=1)
    _write_config(p2, version=1)
    # Add a data_version to p2 — should not change the hash
    raw = yaml.safe_load(p2.read_text())
    raw["data_version"] = "rqis-snapshots/manifests/2026-06-14/manifest.json"
    p2.write_text(yaml.dump(raw))

    assert fingerprint(str(p1)).config_hash == fingerprint(str(p2)).config_hash


def test_fingerprint_detects_logic_change(tmp_path: Path) -> None:
    p1 = _write_config(tmp_path / "a.yaml", weight=1.0)
    p2 = _write_config(tmp_path / "b.yaml", weight=0.5)
    assert fingerprint(str(p1)).config_hash != fingerprint(str(p2)).config_hash


def test_fingerprint_derives_strategy_id(cfg: Path) -> None:
    fp = fingerprint(str(cfg))
    assert fp.strategy_id == "v1_test_strategy"


def test_fingerprint_accepts_explicit_strategy_id(cfg: Path) -> None:
    fp = fingerprint(str(cfg), explicit_strategy_id="v1_custom_id")
    assert fp.strategy_id == "v1_custom_id"


def test_fingerprint_normalises_strategy_id(cfg: Path) -> None:
    fp = fingerprint(str(cfg), explicit_strategy_id="V1 My Strategy")
    assert fp.strategy_id == "v1_my_strategy"


def test_validate_config_rejects_missing_sections() -> None:
    with pytest.raises(ValueError, match="missing required keys"):
        validate_config({"version": 1, "name": "incomplete"})


def test_validate_config_rejects_zero_weight() -> None:
    config = {
        "version": 1, "name": "x",
        "universe": {}, "execution": {},
        "factors": {"mom": {"weight": 0.0, "score_col": "x"}},
        "portfolio": {"n_long": 5},
        "backtest": {"start_date": "2022-01-01", "end_date": "2024-01-01",
                     "initial_capital": 1000.0, "benchmark": "SPY"},
    }
    with pytest.raises(ValueError, match="weights must sum"):
        validate_config(config)


def test_validate_config_rejects_inverted_dates() -> None:
    config = {
        "version": 1, "name": "x",
        "universe": {}, "execution": {},
        "factors": {"mom": {"weight": 1.0, "score_col": "x"}},
        "portfolio": {"n_long": 5},
        "backtest": {"start_date": "2024-01-01", "end_date": "2022-01-01",
                     "initial_capital": 1000.0, "benchmark": "SPY"},
    }
    with pytest.raises(ValueError, match="start_date"):
        validate_config(config)


# ── Definition layer ──────────────────────────────────────────────────────────


def test_add_definition_creates_row(registry: StrategyRegistry, cfg: Path) -> None:
    defn = registry.add_definition(str(cfg))
    assert defn.strategy_id == "v1_test_strategy"
    assert len(defn.config_hash) == 64


def test_add_definition_is_idempotent(registry: StrategyRegistry, cfg: Path) -> None:
    d1 = registry.add_definition(str(cfg))
    d2 = registry.add_definition(str(cfg))
    assert d1.config_hash == d2.config_hash


def test_add_definition_same_version_different_hash_raises(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    p1 = _write_config(tmp_path / "a.yaml", version=1, weight=1.0)
    p2 = _write_config(tmp_path / "b.yaml", version=1, weight=0.5)
    registry.add_definition(str(p1))
    with pytest.raises(DuplicateVersionError):
        registry.add_definition(str(p2))


def test_list_definitions_returns_all_versions(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    # Both configs share the same explicit strategy_id but differ in version+hash.
    p1 = _write_config(tmp_path / "v1.yaml", version=1)
    p2 = _write_config(tmp_path / "v2.yaml", version=2)
    registry.add_definition(str(p1), explicit_strategy_id="my_strategy")
    registry.add_definition(str(p2), explicit_strategy_id="my_strategy")
    defns = registry.list_definitions("my_strategy")
    assert len(defns) == 2
    assert [d.version for d in defns] == [1, 2]


# ── Lifecycle layer — registration ────────────────────────────────────────────


def test_register_creates_strategy_and_definition(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    assert s.strategy_id == "v1_test_strategy"
    assert s.status == StrategyStatus.BACKTESTING
    assert s.canonical_config_hash == fingerprint(str(cfg)).config_hash


def test_register_auto_creates_definition(registry: StrategyRegistry, cfg: Path) -> None:
    s = registry.register(str(cfg))
    defn = registry.get_definition(s.strategy_id, s.canonical_config_hash)
    assert defn.version == 1


def test_register_duplicate_raises(registry: StrategyRegistry, cfg: Path) -> None:
    registry.register(str(cfg))
    with pytest.raises(StrategyAlreadyRegisteredError):
        registry.register(str(cfg))


def test_register_with_family_and_supersedes(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    p1 = _write_config(tmp_path / "v1.yaml", version=1)
    p2 = _write_config(tmp_path / "v2.yaml", version=2)
    registry.register(str(p1), strategy_family="test_strategy")
    s2 = registry.register(
        str(p2),
        strategy_family="test_strategy",
        supersedes_strategy_id="v1_test_strategy",
    )
    assert s2.strategy_family == "test_strategy"
    assert s2.supersedes_strategy_id == "v1_test_strategy"


def test_register_supersedes_nonexistent_raises(
    registry: StrategyRegistry, cfg: Path
) -> None:
    with pytest.raises(StrategyNotFoundError):
        registry.register(str(cfg), supersedes_strategy_id="v0_ghost")


# ── Lifecycle layer — transitions ─────────────────────────────────────────────


def test_transition_backtesting_to_paper(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    s = registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    assert s.status == StrategyStatus.PAPER
    assert s.activated_paper_at is not None


def test_transition_paper_to_live_requires_notes(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    with pytest.raises(MissingOperatorNotesError):
        registry.transition("v1_test_strategy", StrategyStatus.LIVE)


def test_transition_paper_to_live_with_notes(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    s = registry.transition(
        "v1_test_strategy", StrategyStatus.LIVE, operator_notes="C8 clearance granted"
    )
    assert s.status == StrategyStatus.LIVE
    assert s.activated_live_at is not None


def test_transition_archived_is_terminal(registry: StrategyRegistry, cfg: Path) -> None:
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.ARCHIVED)
    with pytest.raises(InvalidTransitionError):
        registry.transition("v1_test_strategy", StrategyStatus.BACKTESTING)


def test_transition_backtesting_to_live_invalid(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    with pytest.raises(InvalidTransitionError):
        registry.transition(
            "v1_test_strategy", StrategyStatus.LIVE, operator_notes="skipping paper"
        )


def test_transition_not_found_raises(registry: StrategyRegistry) -> None:
    with pytest.raises(StrategyNotFoundError):
        registry.transition("ghost", StrategyStatus.PAPER)


def test_transition_conflict_blocks_second_paper(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    p1 = _write_config(tmp_path / "v1.yaml", version=1)
    p2 = _write_config(tmp_path / "v2.yaml", version=2)
    registry.register(str(p1))
    registry.register(str(p2))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    with pytest.raises(ConflictingActiveStrategyError):
        registry.transition("v2_test_strategy", StrategyStatus.PAPER)


def test_step_down_paper_to_backtesting(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    s = registry.transition("v1_test_strategy", StrategyStatus.BACKTESTING)
    assert s.status == StrategyStatus.BACKTESTING


# ── Lifecycle layer — config integrity ───────────────────────────────────────


def test_verify_config_integrity_passes(registry: StrategyRegistry, cfg: Path) -> None:
    registry.register(str(cfg))
    assert registry.verify_config_integrity("v1_test_strategy") is True


def test_verify_config_integrity_logic_change_raises(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    # Mutate strategy logic (weight change → new canonical hash)
    raw = yaml.safe_load(cfg.read_text())
    raw["factors"]["momentum"]["weight"] = 0.5
    cfg.write_text(yaml.dump(raw))
    with pytest.raises(ConfigDriftError):
        registry.verify_config_integrity("v1_test_strategy")


def test_verify_config_integrity_runtime_key_change_passes(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    # Changing data_version is a runtime key — canonical hash must not change
    raw = yaml.safe_load(cfg.read_text())
    raw["data_version"] = "rqis-snapshots/manifests/2026-06-14/manifest.json"
    cfg.write_text(yaml.dump(raw))
    assert registry.verify_config_integrity("v1_test_strategy") is True


# ── List and get ──────────────────────────────────────────────────────────────


def test_list_all(registry: StrategyRegistry, tmp_path: Path) -> None:
    for v in range(1, 4):
        p = _write_config(tmp_path / f"v{v}.yaml", version=v)
        registry.register(str(p))
    assert len(registry.list()) == 3


def test_list_filter_status(registry: StrategyRegistry, tmp_path: Path) -> None:
    p1 = _write_config(tmp_path / "v1.yaml", version=1)
    p2 = _write_config(tmp_path / "v2.yaml", version=2)
    registry.register(str(p1))
    registry.register(str(p2))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    assert len(registry.list(status=StrategyStatus.PAPER)) == 1
    assert len(registry.list(status=StrategyStatus.BACKTESTING)) == 1


def test_list_filter_family(registry: StrategyRegistry, tmp_path: Path) -> None:
    p1 = _write_config(tmp_path / "v1.yaml", version=1, name="alpha")
    p2 = _write_config(tmp_path / "v2.yaml", version=2, name="alpha")
    p3 = _write_config(tmp_path / "v3.yaml", version=1, name="beta")
    registry.register(str(p1), strategy_family="alpha")
    registry.register(str(p2), strategy_family="alpha")
    registry.register(str(p3), strategy_family="beta")
    alpha = registry.list(strategy_family="alpha")
    assert len(alpha) == 2
    assert all(s.strategy_family == "alpha" for s in alpha)


def test_get_not_found_raises(registry: StrategyRegistry) -> None:
    with pytest.raises(StrategyNotFoundError):
        registry.get("ghost")


# ── Run recording layer ───────────────────────────────────────────────────────


def test_record_run_requires_definition(registry: StrategyRegistry) -> None:
    with pytest.raises(DefinitionNotFoundError):
        registry.record_run("v1_test_strategy", "a" * 64, "backtest", "passed",
                            data_version="snap/v1")


def test_record_run_backtest_requires_data_version(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    with pytest.raises(MissingDataVersionError):
        registry.record_run(s.strategy_id, s.canonical_config_hash, "backtest", "passed")


def test_record_run_backtest_success(registry: StrategyRegistry, cfg: Path) -> None:
    s = registry.register(str(cfg))
    run = registry.record_run(
        strategy_id=s.strategy_id,
        config_hash=s.canonical_config_hash,
        run_type="backtest",
        status="passed",
        data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        metrics={"sharpe_ratio": 0.82, "annualized_return": 0.14, "max_drawdown": -0.09},
        mlflow_run_id="abc123",
    )
    assert run.id is not None
    assert run.metrics["sharpe_ratio"] == 0.82


def test_record_run_signal_ic_no_data_version_required(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    run = registry.record_run(
        strategy_id=s.strategy_id,
        config_hash=s.canonical_config_hash,
        run_type="signal_ic",
        status="passed",
        metrics={"ic_mean": 0.05, "ic_ir": 0.8},
    )
    assert run.run_type == "signal_ic"


def test_record_run_pre_registration(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Runs can be recorded before formal register() is called."""
    defn = registry.add_definition(str(cfg))
    run = registry.record_run(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        run_type="signal_ic",
        status="passed",
        metrics={"ic_mean": 0.04},
    )
    assert run.strategy_id == defn.strategy_id


def test_get_runs_filters(registry: StrategyRegistry, cfg: Path) -> None:
    s = registry.register(str(cfg))
    registry.record_run(s.strategy_id, s.canonical_config_hash, "signal_ic", "passed",
                        metrics={"ic": 0.05})
    registry.record_run(s.strategy_id, s.canonical_config_hash, "backtest", "passed",
                        data_version="snap/v1", metrics={"sharpe": 0.8})
    registry.record_run(s.strategy_id, s.canonical_config_hash, "backtest", "failed",
                        data_version="snap/v2")

    all_runs = registry.get_runs(s.strategy_id)
    assert len(all_runs) == 3

    backtests = registry.get_runs(s.strategy_id, run_type="backtest")
    assert len(backtests) == 2

    passed_backtests = registry.get_runs(s.strategy_id, run_type="backtest", status="passed")
    assert len(passed_backtests) == 1
    assert passed_backtests[0].metrics["sharpe"] == 0.8

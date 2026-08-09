"""
Unit tests for StrategyRegistry — uses SQLite in-memory; no external services.
Covers the definition layer, lifecycle layer, and run recording layer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from strategy_registry.fingerprint import fingerprint, recompute_hash, validate_config
from strategy_registry.registry import (
    ConfigDriftError,
    ConflictingActiveStrategyError,
    DefinitionNotFoundError,
    DuplicateVersionError,
    FingerprintAlgorithmVersionError,
    InsufficientPaperQualificationError,
    InvalidTransitionError,
    MissingDataVersionError,
    MissingOperatorNotesError,
    RunLifecycleMismatchError,
    StrategyAlreadyRegisteredError,
    StrategyNotFoundError,
    StrategyRegistry,
    StrategyStatus,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _write_config(
    path: Path,
    *,
    version: int = 1,
    name: str = "test_strategy",
    weight: float = 1.0,
    start_date: str = "2022-01-01",
    end_date: str = "2024-12-31",
    initial_capital: float = 1000000.0,
    n_long: int = 10,
) -> Path:
    """Write a valid, complete strategy YAML."""
    config = {
        "version": version,
        "name": name,
        "description": f"Test strategy v{version}",
        "universe": {"source": "sp500"},
        "indicators": {"momentum": {"weight": weight, "score_col": "momentum_score"}},
        "portfolio": {"method": "equal_weight", "n_long": n_long, "max_position_weight": 0.1},
        "execution": {"fill_model": "perfect"},
        "backtest": {
            "start_date": start_date,
            "end_date": end_date,
            "initial_capital": initial_capital,
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


# ── identity vs. evaluation context (docs/plans/04-identity-evaluation-
#    context-design.md, operator decision 2026-08-07, Option 1) ────────────


def test_fingerprint_excludes_backtest_window_from_hash(tmp_path: Path) -> None:
    """Two configs differing ONLY in backtest.start_date/end_date must hash
    IDENTICALLY -- the evaluation window is context, not identity, so the
    same frozen config_hash can be evaluated over train/OOS dates and then
    again over the sealed holdout window."""
    p1 = _write_config(
        tmp_path / "a.yaml", start_date="2022-01-01", end_date="2022-12-31"
    )
    p2 = _write_config(
        tmp_path / "b.yaml", start_date="2023-06-01", end_date="2024-03-31"
    )
    assert fingerprint(str(p1)).config_hash == fingerprint(str(p2)).config_hash


def test_fingerprint_still_retains_backtest_dates_in_stored_config(tmp_path: Path) -> None:
    """The date fields must remain in StrategyFingerprint.config (still
    consumed/validated by backtesting.config_contract) -- only the HASH
    excludes them, not the stored/returned canonical config."""
    p = _write_config(tmp_path / "a.yaml", start_date="2022-01-01", end_date="2022-12-31")
    fp = fingerprint(str(p))
    assert fp.config["backtest"]["start_date"] == "2022-01-01"
    assert fp.config["backtest"]["end_date"] == "2022-12-31"


def test_fingerprint_still_detects_real_param_change(tmp_path: Path) -> None:
    """A real identity difference (portfolio.n_long) must still change the
    hash -- the window exclusion must not blunt detection of genuine param
    changes."""
    p1 = _write_config(tmp_path / "a.yaml", n_long=10)
    p2 = _write_config(tmp_path / "b.yaml", n_long=20)
    assert fingerprint(str(p1)).config_hash != fingerprint(str(p2)).config_hash


def test_fingerprint_still_detects_initial_capital_change(tmp_path: Path) -> None:
    """backtest.initial_capital is a SIBLING of the two excluded date keys,
    not itself excluded -- it remains part of identity."""
    p1 = _write_config(tmp_path / "a.yaml", initial_capital=1_000_000.0)
    p2 = _write_config(tmp_path / "b.yaml", initial_capital=2_000_000.0)
    assert fingerprint(str(p1)).config_hash != fingerprint(str(p2)).config_hash


def test_hash_config_excludes_backtest_window_and_data_version(tmp_path: Path) -> None:
    """hash_config (the in-memory dict entry point TrialRecorder/
    PromotionPipeline use) applies the same exclusions as fingerprint()."""
    from strategy_registry.fingerprint import hash_config

    base = yaml.safe_load(_write_config(tmp_path / "base.yaml").read_text())
    windowed = dict(base)
    windowed["backtest"] = dict(base["backtest"])
    windowed["backtest"]["start_date"] = "2020-01-01"
    windowed["backtest"]["end_date"] = "2021-01-01"
    windowed["data_version"] = "a" * 64

    assert hash_config(base) == hash_config(windowed)


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
        "indicators": {"mom": {"weight": 0.0, "score_col": "x"}},
        "portfolio": {"n_long": 5},
        "backtest": {"start_date": "2022-01-01", "end_date": "2024-01-01",
                     "initial_capital": 1000.0, "benchmark": "SPY"},
    }
    with pytest.raises(ValueError, match="weights must sum"):
        validate_config(config)


def test_validate_config_rejects_float_n_long() -> None:
    """n_long: 10.5 must be rejected to prevent hash/metadata mismatch."""
    config = {
        "version": 1, "name": "x",
        "universe": {}, "execution": {},
        "indicators": {"mom": {"weight": 1.0, "score_col": "x"}},
        "portfolio": {"n_long": 10.5},
        "backtest": {"start_date": "2022-01-01", "end_date": "2024-01-01",
                     "initial_capital": 1000.0, "benchmark": "SPY"},
    }
    with pytest.raises(ValueError, match="n_long must be a positive integer"):
        validate_config(config)


def test_validate_config_rejects_inverted_dates() -> None:
    config = {
        "version": 1, "name": "x",
        "universe": {}, "execution": {},
        "indicators": {"mom": {"weight": 1.0, "score_col": "x"}},
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


# ── F3 (2026-08-08 adversarial review): pin the INTENDED post-cb9b4f3-
#    deletion behavior at the exact site the deleted reuse guard used to
#    block ──────────────────────────────────────────────────────────────────
#
# cb9b4f3's EvaluationWindowConflictError/_assert_reuse_config_matches()
# guard (deliberately NOT ported into this slice, per the 04-4W brief) used
# to fail-closed on "same identity, different evaluation window" reuse.
# Once the window is excluded from config_hash (a000e87) and threaded as an
# explicit per-measurement input instead (this slice), that reuse is LEGAL
# and EXPECTED -- two configs differing ONLY in backtest.start_date/end_date
# hash identically, so add_definition()/register() must treat the second
# call as an idempotent no-op returning the SAME row, not raise. Nothing
# pinned that contract until now.


def test_add_definition_same_content_different_dates_returns_same_row(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    p1 = _write_config(tmp_path / "a.yaml", start_date="2022-01-01", end_date="2022-12-31")
    p2 = _write_config(tmp_path / "b.yaml", start_date="2023-06-01", end_date="2024-03-31")
    d1 = registry.add_definition(str(p1))
    d2 = registry.add_definition(str(p2))
    assert d1.config_hash == d2.config_hash
    assert d1.strategy_id == d2.strategy_id
    # Genuinely the SAME row (not merely two rows with equal hashes) --
    # only one strategy_definitions row exists for this identity.
    assert len(registry.list_definitions(d1.strategy_id)) == 1


def test_register_same_content_different_dates_returns_same_strategy(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    """register() reaches the same idempotent-reuse path via
    session.get(StrategyDefinition, (strategy_id, config_hash)) when
    auto-creating the definition -- a second register() attempt for an
    ALREADY-registered strategy_id still raises StrategyAlreadyRegisteredError
    (permanent strategy_id, unrelated to this contract), so this test
    exercises add_definition() twice with different-dated configs sharing
    one identity, then register()s once -- proving the definition layer's
    reuse-on-identity-match behavior register() itself relies on."""
    p1 = _write_config(tmp_path / "a.yaml", start_date="2022-01-01", end_date="2022-12-31")
    p2 = _write_config(tmp_path / "b.yaml", start_date="2023-06-01", end_date="2024-03-31")
    d1 = registry.add_definition(str(p1))
    d2 = registry.add_definition(str(p2))
    assert d1.config_hash == d2.config_hash

    strategy = registry.register(str(p2))
    assert strategy.canonical_config_hash == d1.config_hash
    assert len(registry.list_definitions(strategy.strategy_id)) == 1


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


def test_transition_to_live_rejects_whitespace_operator_notes(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Whitespace-only notes must not bypass the C8 documentation gate."""
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    with pytest.raises(MissingOperatorNotesError):
        registry.transition("v1_test_strategy", StrategyStatus.LIVE, operator_notes="   ")


def test_transition_to_live_requires_paper_run(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """C8: promote to live without any paper runs must be blocked."""
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    with pytest.raises(InsufficientPaperQualificationError):
        registry.transition(
            "v1_test_strategy", StrategyStatus.LIVE, operator_notes="C8 clearance granted"
        )


def test_transition_paper_to_live_with_notes(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    s = registry.get("v1_test_strategy")
    # Record a passed paper run to satisfy the C8 gate.
    registry.record_run(
        s.strategy_id, s.canonical_config_hash, "paper", "passed", metrics={}
    )
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
    raw["indicators"]["momentum"]["weight"] = 0.5
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
                            data_version="snap/v1",
                            eval_start_date=date(2022, 1, 1), eval_end_date=date(2022, 12, 31))


def test_record_run_backtest_requires_data_version(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    with pytest.raises(MissingDataVersionError):
        registry.record_run(s.strategy_id, s.canonical_config_hash, "backtest", "passed")


def test_record_run_walk_forward_requires_data_version(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """walk_forward must also enforce the C7 data_version requirement."""
    s = registry.register(str(cfg))
    with pytest.raises(MissingDataVersionError):
        registry.record_run(s.strategy_id, s.canonical_config_hash, "walk_forward", "passed")


def test_record_run_rejects_whitespace_data_version(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Whitespace-only data_version must not bypass the C7 gate."""
    s = registry.register(str(cfg))
    with pytest.raises(MissingDataVersionError):
        registry.record_run(
            s.strategy_id, s.canonical_config_hash, "backtest", "passed", data_version="   "
        )


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
        eval_start_date=date(2022, 1, 1),
        eval_end_date=date(2022, 12, 31),
    )
    assert run.id is not None
    assert run.metrics["sharpe_ratio"] == 0.82
    assert run.eval_start_date == date(2022, 1, 1)
    assert run.eval_end_date == date(2022, 12, 31)


def test_record_run_backtest_requires_eval_window(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """04-4W Phase W3: backtest/walk_forward runs must carry an explicit
    eval_start_date/eval_end_date, mirroring the C7 data_version gate --
    otherwise two runs over different windows recorded under the same
    (strategy_id, config_hash, data_version) are indistinguishable now that
    the window is excluded from config_hash."""
    s = registry.register(str(cfg))
    with pytest.raises(ValueError, match="eval_start_date and eval_end_date are required"):
        registry.record_run(
            s.strategy_id,
            s.canonical_config_hash,
            "backtest",
            "passed",
            data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
        )


def test_record_run_rejects_reversed_eval_window(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    with pytest.raises(ValueError, match="reversed evaluation window"):
        registry.record_run(
            s.strategy_id,
            s.canonical_config_hash,
            "backtest",
            "passed",
            data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
            eval_start_date=date(2022, 12, 31),
            eval_end_date=date(2022, 1, 1),
        )


# ── PR #50 Codex round-5 (P2): record_run must reject datetime/string
#    eval_start_date/eval_end_date, not just accept them via a bare `>`
#    comparison -- the same gap EvaluationWindow was hardened against,
#    found independently here since record_run is a second, direct entry
#    point that does not go through EvaluationWindow ──────────────────────


def test_record_run_rejects_datetime_eval_start_date(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """datetime subclasses date, so a bare `>` comparison alone would
    accept it and only fail later at the DB Date-column write."""
    from datetime import datetime

    s = registry.register(str(cfg))
    with pytest.raises(TypeError, match="datetime.date"):
        registry.record_run(
            s.strategy_id,
            s.canonical_config_hash,
            "backtest",
            "passed",
            data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
            eval_start_date=datetime(2022, 1, 1),  # type: ignore[arg-type]
            eval_end_date=date(2022, 12, 31),
        )


def test_record_run_rejects_datetime_eval_end_date(
    registry: StrategyRegistry, cfg: Path
) -> None:
    from datetime import datetime

    s = registry.register(str(cfg))
    with pytest.raises(TypeError, match="datetime.date"):
        registry.record_run(
            s.strategy_id,
            s.canonical_config_hash,
            "backtest",
            "passed",
            data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
            eval_start_date=date(2022, 1, 1),
            eval_end_date=datetime(2022, 12, 31),  # type: ignore[arg-type]
        )


def test_record_run_rejects_string_eval_dates(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Two ISO strings compare lexicographically the same way two dates
    would, so the bare `>` comparison alone would not catch this either."""
    s = registry.register(str(cfg))
    with pytest.raises(TypeError, match="datetime.date"):
        registry.record_run(
            s.strategy_id,
            s.canonical_config_hash,
            "backtest",
            "passed",
            data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
            eval_start_date="2022-01-01",  # type: ignore[arg-type]
            eval_end_date=date(2022, 12, 31),
        )


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


# ── PR #50 Codex round-6 (P2): eval_start_date/eval_end_date are OPTIONAL
#    for non-window-scoped run types, but if a caller supplies them at all
#    they must still be validated -- an incomplete pair or a reversed/
#    mistyped pair was previously persisted silently ─────────────────────


def test_record_run_optional_run_type_accepts_no_eval_window(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Baseline: omitting both eval dates entirely for an optional run_type
    still works (unaffected by the round-6 fix)."""
    s = registry.register(str(cfg))
    run = registry.record_run(
        strategy_id=s.strategy_id,
        config_hash=s.canonical_config_hash,
        run_type="signal_ic",
        status="passed",
        metrics={"ic_mean": 0.05},
    )
    assert run.eval_start_date is None
    assert run.eval_end_date is None


def test_record_run_optional_run_type_accepts_full_eval_window(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Baseline: supplying a valid, complete pair for an optional run_type
    still works."""
    s = registry.register(str(cfg))
    run = registry.record_run(
        strategy_id=s.strategy_id,
        config_hash=s.canonical_config_hash,
        run_type="signal_ic",
        status="passed",
        metrics={"ic_mean": 0.05},
        eval_start_date=date(2022, 1, 1),
        eval_end_date=date(2022, 12, 31),
    )
    assert run.eval_start_date == date(2022, 1, 1)
    assert run.eval_end_date == date(2022, 12, 31)


def test_record_run_optional_run_type_rejects_incomplete_eval_window_start_only(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    with pytest.raises(ValueError, match="must both be provided or both omitted"):
        registry.record_run(
            strategy_id=s.strategy_id,
            config_hash=s.canonical_config_hash,
            run_type="signal_ic",
            status="passed",
            metrics={"ic_mean": 0.05},
            eval_start_date=date(2022, 1, 1),
        )


def test_record_run_optional_run_type_rejects_incomplete_eval_window_end_only(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    with pytest.raises(ValueError, match="must both be provided or both omitted"):
        registry.record_run(
            strategy_id=s.strategy_id,
            config_hash=s.canonical_config_hash,
            run_type="signal_ic",
            status="passed",
            metrics={"ic_mean": 0.05},
            eval_end_date=date(2022, 12, 31),
        )


def test_record_run_optional_run_type_rejects_reversed_eval_window(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Codex's exact example: record_run(..., run_type="paper",
    eval_start_date after eval_end_date) previously persisted a reversed
    range silently, since the reversed-window check only ran inside the
    required-run-type branch."""
    s = registry.register(str(cfg))
    with pytest.raises(ValueError, match="reversed evaluation window"):
        registry.record_run(
            strategy_id=s.strategy_id,
            config_hash=s.canonical_config_hash,
            run_type="signal_ic",
            status="passed",
            metrics={"ic_mean": 0.05},
            eval_start_date=date(2026, 8, 8),
            eval_end_date=date(2026, 8, 1),
        )


def test_record_run_optional_run_type_rejects_datetime_eval_dates(
    registry: StrategyRegistry, cfg: Path
) -> None:
    from datetime import datetime

    s = registry.register(str(cfg))
    with pytest.raises(TypeError, match="datetime.date"):
        registry.record_run(
            strategy_id=s.strategy_id,
            config_hash=s.canonical_config_hash,
            run_type="signal_ic",
            status="passed",
            metrics={"ic_mean": 0.05},
            eval_start_date=datetime(2022, 1, 1),  # type: ignore[arg-type]
            eval_end_date=date(2022, 12, 31),
        )


def test_record_run_pre_registration(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Research-type runs (signal_ic, backtest) can be recorded before register()."""
    defn = registry.add_definition(str(cfg))
    run = registry.record_run(
        strategy_id=defn.strategy_id,
        config_hash=defn.config_hash,
        run_type="signal_ic",
        status="passed",
        metrics={"ic_mean": 0.04},
    )
    assert run.strategy_id == defn.strategy_id


def test_record_run_paper_blocked_when_strategy_in_backtesting(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Paper run_type must be blocked when the strategy is in backtesting status."""
    s = registry.register(str(cfg))
    with pytest.raises(RunLifecycleMismatchError):
        registry.record_run(
            s.strategy_id, s.canonical_config_hash, "paper", "passed",
            metrics={"pnl": 0.0},
        )


def test_record_run_paper_blocked_when_no_lifecycle_row(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Pre-registration paper runs must be blocked (no lifecycle row → can't qualify C8)."""
    defn = registry.add_definition(str(cfg))
    with pytest.raises(RunLifecycleMismatchError):
        registry.record_run(defn.strategy_id, defn.config_hash, "paper", "passed")


def test_record_run_live_blocked_when_no_lifecycle_row(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Pre-registration live runs must be blocked."""
    defn = registry.add_definition(str(cfg))
    with pytest.raises(RunLifecycleMismatchError):
        registry.record_run(defn.strategy_id, defn.config_hash, "live", "passed")


def test_record_run_paper_allowed_when_strategy_in_paper(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    s = registry.get("v1_test_strategy")
    run = registry.record_run(
        s.strategy_id, s.canonical_config_hash, "paper", "passed",
        metrics={"pnl": 0.0},
    )
    assert run.run_type == "paper"


def test_record_run_live_blocked_when_strategy_in_paper(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Live run_type must be blocked when the strategy is in paper status."""
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    s = registry.get("v1_test_strategy")
    with pytest.raises(RunLifecycleMismatchError):
        registry.record_run(
            s.strategy_id, s.canonical_config_hash, "live", "passed",
            metrics={"pnl": 0.0},
        )


def test_record_run_live_allowed_when_strategy_in_live(
    registry: StrategyRegistry, cfg: Path
) -> None:
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    s = registry.get("v1_test_strategy")
    registry.record_run(s.strategy_id, s.canonical_config_hash, "paper", "passed")
    registry.transition("v1_test_strategy", StrategyStatus.LIVE, operator_notes="C8 cleared")
    run = registry.record_run(
        s.strategy_id, s.canonical_config_hash, "live", "passed", metrics={"pnl": 0.01}
    )
    assert run.run_type == "live"


def test_record_run_rejects_invalid_run_type(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    with pytest.raises(ValueError, match="run_type must be one of"):
        registry.record_run(s.strategy_id, s.canonical_config_hash, "invalid_type", "passed")


def test_record_run_rejects_invalid_status(
    registry: StrategyRegistry, cfg: Path
) -> None:
    s = registry.register(str(cfg))
    with pytest.raises(ValueError, match="status must be one of"):
        registry.record_run(s.strategy_id, s.canonical_config_hash, "signal_ic", "ok")


def test_definition_runs_accessible_after_session_close(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """StrategyDefinition.runs must be loaded (not lazy) after session close."""
    defn = registry.add_definition(str(cfg))
    # New definition has no runs — should return an empty list, not raise.
    assert defn.runs == []


def test_status_history_accessible_after_session_close(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """Returned Strategy objects must have status_history loaded (not lazy)."""
    registry.register(str(cfg))
    registry.transition("v1_test_strategy", StrategyStatus.PAPER)
    s = registry.get("v1_test_strategy")
    # Accessing status_history must not raise DetachedInstanceError.
    assert len(s.status_history) == 2  # backtesting + paper


def test_get_runs_filters(registry: StrategyRegistry, cfg: Path) -> None:
    s = registry.register(str(cfg))
    registry.record_run(s.strategy_id, s.canonical_config_hash, "signal_ic", "passed",
                        metrics={"ic": 0.05})
    registry.record_run(s.strategy_id, s.canonical_config_hash, "backtest", "passed",
                        data_version="snap/v1", metrics={"sharpe": 0.8},
                        eval_start_date=date(2022, 1, 1), eval_end_date=date(2022, 12, 31))
    registry.record_run(s.strategy_id, s.canonical_config_hash, "backtest", "failed",
                        data_version="snap/v2",
                        eval_start_date=date(2023, 1, 1), eval_end_date=date(2023, 12, 31))

    all_runs = registry.get_runs(s.strategy_id)
    assert len(all_runs) == 3

    backtests = registry.get_runs(s.strategy_id, run_type="backtest")
    assert len(backtests) == 2

    passed_backtests = registry.get_runs(s.strategy_id, run_type="backtest", status="passed")
    assert len(passed_backtests) == 1


# ── selection-schema table registration (Codex round-3 P2) ────────────────────


def test_public_registry_setup_creates_selection_schema_tables(
    registry: StrategyRegistry,
) -> None:
    """StrategyRegistry(...) is the public setup path used by the CLI and by
    downstream callers (TrialRecorder/PromotionPipeline). It must create the
    four selection-schema tables (strategy_registry/selection_models.py) via
    Base.metadata.create_all(), not just the four base tables -- even though
    nothing on this path imports selection_models directly.
    """
    from sqlalchemy import inspect

    table_names = set(inspect(registry._engine).get_table_names())
    for expected in (
        "strategy_hypotheses",
        "strategy_trials",
        "research_data_windows",
        "promotion_decisions",
    ):
        assert expected in table_names, f"{expected} missing from public registry setup"


# ── FingerprintAlgorithmVersionError precedence (04-4W PM amendment A1) ────────
#
# fingerprint_algo_version must be load-bearing on every read path that
# compares a stored config_hash against a freshly computed one -- otherwise
# a pre-v2 row silently misdiagnoses as C6 drift (verify_config_integrity)
# or a genuine config variant (DuplicateVersionError), when the real cause
# is that the fingerprint algorithm changed underneath unmodified content.


def _downgrade_fingerprint_algo_version(
    registry: StrategyRegistry, strategy_id: str, config_hash: str, *, version: int = 1
) -> None:
    """Simulate a pre-04-4W legacy row by forcing its fingerprint_algo_version
    back to an older value directly in the DB, bypassing the application
    (which always writes the current FINGERPRINT_ALGO_VERSION)."""
    from sqlalchemy.orm import Session as _Session

    from strategy_registry.models import StrategyDefinition as _StrategyDefinition

    with _Session(registry._engine) as session:
        defn = session.get(_StrategyDefinition, (strategy_id, config_hash))
        defn.fingerprint_algo_version = version
        session.commit()


def test_verify_config_integrity_raises_algo_version_error_not_drift_for_legacy_row(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """A v1-algorithm-hashed row, re-checked against UNMODIFIED YAML, must
    raise FingerprintAlgorithmVersionError -- NOT ConfigDriftError. The YAML
    never changed; only the fingerprint algorithm did (a000e87 excluded
    backtest.start_date/end_date from identity). Diagnosing this as C6
    drift would send an operator to author a needless new YAML version."""
    s = registry.register(str(cfg))
    _downgrade_fingerprint_algo_version(registry, s.strategy_id, s.canonical_config_hash, version=1)

    with pytest.raises(FingerprintAlgorithmVersionError, match="fingerprint algorithm v1"):
        registry.verify_config_integrity(s.strategy_id)


def test_verify_config_integrity_still_raises_drift_for_current_algo_row(
    registry: StrategyRegistry, cfg: Path
) -> None:
    """A genuinely-drifted row hashed under the CURRENT algorithm must still
    raise ConfigDriftError -- the version-precedence check must not mask
    real C6 violations."""
    registry.register(str(cfg))
    raw = yaml.safe_load(cfg.read_text())
    raw["indicators"]["momentum"]["weight"] = 0.5
    cfg.write_text(yaml.dump(raw))
    with pytest.raises(ConfigDriftError):
        registry.verify_config_integrity("v1_test_strategy")


def _seed_legacy_definition_row(
    registry: StrategyRegistry,
    *,
    strategy_id: str,
    config_hash: str,
    name: str,
    version: int,
) -> None:
    """Directly insert a synthetic pre-04-4W StrategyDefinition row --
    fingerprint_algo_version=1, an ARBITRARY config_hash distinct from
    anything the current (v2) algorithm would ever produce for real content
    -- bypassing fingerprint()/add_definition() entirely (fingerprint()
    always computes under the current algorithm; there is no way to
    produce a genuine v1-shaped hash through current code, since a000e87
    permanently changed _identity_view). This is the only way to construct
    a real uq_strategy_definitions_version collision (same strategy_id +
    version, DIFFERENT config_hash) whose existing side is a legacy row,
    which is what the add_definition/register collision-handling tests
    below need to exercise."""
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session as _Session

    from strategy_registry.models import StrategyDefinition as _StrategyDefinition

    with _Session(registry._engine) as session:
        session.add(
            _StrategyDefinition(
                strategy_id=strategy_id,
                config_hash=config_hash,
                name=name,
                version=version,
                config={"legacy": True},
                fingerprint_algo_version=1,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()


def test_add_definition_version_collision_raises_algo_version_error_for_legacy_row(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    """A genuine uq_strategy_definitions_version collision (same
    strategy_id + version, different config_hash) whose EXISTING row is a
    pre-v2 legacy fingerprint must raise FingerprintAlgorithmVersionError,
    not DuplicateVersionError -- the collision may be an algorithm-version
    artifact rather than a genuine second config variant, and "bump
    version" is the wrong remedy until that is ruled out."""
    p = _write_config(tmp_path / "v1.yaml", version=1)
    fp = fingerprint(str(p))
    _seed_legacy_definition_row(
        registry,
        strategy_id=fp.strategy_id,
        config_hash="f" * 64,  # arbitrary, distinct from fp.config_hash
        name=fp.name,
        version=fp.version,
    )

    with pytest.raises(FingerprintAlgorithmVersionError, match="fingerprint algorithm v1"):
        registry.add_definition(str(p))


def test_add_definition_version_collision_still_raises_duplicate_for_genuine_variant(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    """Two genuinely different configs sharing a version (both hashed under
    the current algorithm) must still raise DuplicateVersionError -- the
    version-precedence check must not mask a real variant collision."""
    p1 = _write_config(tmp_path / "v1a.yaml", version=1, weight=1.0)
    p2 = _write_config(tmp_path / "v1b.yaml", version=1, weight=0.5)
    registry.add_definition(str(p1))
    with pytest.raises(DuplicateVersionError):
        registry.add_definition(str(p2))


def test_register_version_collision_raises_algo_version_error_for_legacy_row(
    registry: StrategyRegistry, tmp_path: Path
) -> None:
    """Same as the add_definition case above, but through register()'s
    flush()-time collision path. Reachable because the seeded legacy row
    has no corresponding Strategy lifecycle row, so register() passes its
    StrategyAlreadyRegisteredError check and proceeds to the definition
    insert, which collides on uq_strategy_definitions_version."""
    p = _write_config(tmp_path / "v1.yaml", version=1, name="alpha")
    fp = fingerprint(str(p))
    _seed_legacy_definition_row(
        registry,
        strategy_id=fp.strategy_id,
        config_hash="e" * 64,
        name=fp.name,
        version=fp.version,
    )

    with pytest.raises(FingerprintAlgorithmVersionError, match="fingerprint algorithm v1"):
        registry.register(str(p))

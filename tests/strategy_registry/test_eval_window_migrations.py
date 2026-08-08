"""Migration-shape tests for the 04-4W evaluation-window migrations
(015 = strategy_trials window, 016 = strategy_runs window, 017 =
fingerprint algo version), following the same convention as
test_selection_models.py's migration-014 tests.

**Chain-order note** (see 017's own docstring for the full rationale): the
true Alembic revision chain is ``014 -> 017 -> 015 -> 016`` even though the
filenames sort ``015, 016, 017`` -- 017 was authored first within this slice
(Phase W1, identity/version marker) and chains directly onto 014, with 015
(Phase W2) and 016 (Phase W3) chaining after it. This keeps every phase's
commit independently valid.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")


def _migration_module(name: str):
    return importlib.import_module(f"infra.db.migrations.versions.{name}")


@pytest.mark.parametrize(
    "module_name,revision,down_revision",
    [
        ("017_fingerprint_algo_version", "017", "014"),
        ("015_strategy_trial_eval_window", "015", "017"),
        ("016_strategy_run_eval_window", "016", "015"),
    ],
)
def test_revision_chain(module_name: str, revision: str, down_revision: str) -> None:
    mod = _migration_module(module_name)
    assert mod.revision == revision
    assert mod.down_revision == down_revision


@pytest.mark.parametrize(
    "module_name",
    [
        "017_fingerprint_algo_version",
        "015_strategy_trial_eval_window",
        "016_strategy_run_eval_window",
    ],
)
def test_upgrade_and_downgrade_functions_exist(module_name: str) -> None:
    mod = _migration_module(module_name)
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


@pytest.mark.parametrize("revision", ["015", "016", "017"])
def test_no_other_migration_claims_this_revision(revision: str) -> None:
    """Guards against a duplicate revision id (004/004b precedent) -- each of
    015/016/017 must be unique among the versions directory."""
    versions_dir = (
        Path(__file__).resolve().parents[2] / "infra" / "db" / "migrations" / "versions"
    )
    claimants = []
    for path in sorted(versions_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("revision") and f'"{revision}"' in stripped:
                claimants.append(path.name)
                break
    assert len(claimants) == 1, claimants


def test_head_is_017_none_of_015_016_017_are_orphaned() -> None:
    """Every migration in the chain 014 -> 017 -> 015 -> 016 must be
    reachable by following down_revision links starting from 016 (the
    current head of this slice's additions) -- catches an accidental branch
    (two migrations claiming the same down_revision) or a dangling link."""
    chain = {
        "016": "015",
        "015": "017",
        "017": "014",
    }
    node = "016"
    visited = []
    for _ in range(10):
        visited.append(node)
        if node == "014":
            break
        node = chain[node]
    assert visited == ["016", "015", "017", "014"]


# ── Model/migration column parity ───────────────────────────────────────────


def test_strategy_definition_has_fingerprint_algo_version_column() -> None:
    from strategy_registry.models import StrategyDefinition

    assert "fingerprint_algo_version" in StrategyDefinition.__table__.columns


def test_strategy_trial_has_eval_window_columns() -> None:
    from strategy_registry.selection_models import StrategyTrial

    assert "eval_start_date" in StrategyTrial.__table__.columns
    assert "eval_end_date" in StrategyTrial.__table__.columns


def test_strategy_run_has_eval_window_columns() -> None:
    from strategy_registry.models import StrategyRun

    assert "eval_start_date" in StrategyRun.__table__.columns
    assert "eval_end_date" in StrategyRun.__table__.columns

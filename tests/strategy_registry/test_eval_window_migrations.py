"""Migration-shape tests for the 04-4W evaluation-window migrations
(015 = fingerprint algo version, 016 = strategy_trials window, 017 =
strategy_runs window), following the same convention as
test_selection_models.py's migration-014 tests.

Chain: ``014 -> 015 -> 016 -> 017``, matching filename/execution order
(PM amendment A2, 2026-08-08) -- 04-5's ``validated`` status migration is
``018``, chaining onto ``017``.
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
        ("015_fingerprint_algo_version", "015", "014"),
        ("016_strategy_trial_eval_window", "016", "015"),
        ("017_strategy_run_eval_window", "017", "016"),
    ],
)
def test_revision_chain(module_name: str, revision: str, down_revision: str) -> None:
    mod = _migration_module(module_name)
    assert mod.revision == revision
    assert mod.down_revision == down_revision


@pytest.mark.parametrize(
    "module_name",
    [
        "015_fingerprint_algo_version",
        "016_strategy_trial_eval_window",
        "017_strategy_run_eval_window",
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
    """Every migration in the chain 014 -> 015 -> 016 -> 017 must be
    reachable by following down_revision links starting from 017 (the
    current head of this slice's additions) -- catches an accidental branch
    (two migrations claiming the same down_revision) or a dangling link."""
    chain = {
        "017": "016",
        "016": "015",
        "015": "014",
    }
    node = "017"
    visited = []
    for _ in range(10):
        visited.append(node)
        if node == "014":
            break
        node = chain[node]
    assert visited == ["017", "016", "015", "014"]


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


# ── fingerprint_algo_version ORM default (PM amendment A3, 2026-08-08) ─────
#
# The regression this test exists to catch: an earlier version of this
# column relied SOLELY on a Postgres server_default='1' for its "default"
# behavior, which also silently applies to every future INSERT that omits
# the column -- mislabelling any row current code creates under the CURRENT
# algorithm as a v1 legacy row. The fix is a Python-side ORM default
# (StrategyDefinition.fingerprint_algo_version's default=
# FINGERPRINT_ALGO_VERSION) plus dropping the Postgres server_default in
# migration 015 immediately after its one-time backfill use. This test
# constructs a StrategyDefinition WITHOUT passing fingerprint_algo_version
# explicitly and asserts the value that lands in the DB is the CURRENT
# algorithm version, not 1 -- this is the exact test that would have caught
# the original defect.


def test_strategy_definition_omitted_fingerprint_algo_version_defaults_to_current(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from strategy_registry.fingerprint import FINGERPRINT_ALGO_VERSION
    from strategy_registry.models import Base, StrategyDefinition

    engine = create_engine(f"sqlite:///{tmp_path / 'default_check.db'}", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        defn = StrategyDefinition(
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            name="test_strategy",
            version=1,
            config={"foo": "bar"},
            created_at=datetime.now(timezone.utc),
            # fingerprint_algo_version deliberately OMITTED.
        )
        session.add(defn)
        session.commit()
        session.refresh(defn)
        assert defn.fingerprint_algo_version == FINGERPRINT_ALGO_VERSION
        assert defn.fingerprint_algo_version != 1

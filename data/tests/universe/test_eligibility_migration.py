"""Migration structure + upgrade/downgrade shape test for migration 013
(universe_eligibility_batches, universe_eligibility_attributes).

Mirrors this project's existing migration-testing convention (documented in
Worklog.md and used throughout data/tests/universe/): Alembic migrations
carry Postgres-only DDL (``CREATE EXTENSION``, ``EXCLUDE USING gist``) that
only ever runs against a real Postgres instance -- unit tests never execute
raw SQL against a live DB. Instead:

1. This file asserts the migration module's revision chain is correct and
   its ``upgrade``/``downgrade`` functions are well-formed (importable,
   correct signature) without executing any DDL.
2. The SQLAlchemy ORM models in ``data/universe/models.py`` -- which mirror
   the migration's table/column/constraint shape by convention (see that
   module's docstring) -- are exercised end-to-end via
   ``Base.metadata.create_all``/``drop_all`` against SQLite, standing in for
   "upgrade" (create) and "downgrade" (drop). This is the same pattern
   ``data/tests/universe/test_import_pipeline.py`` and
   ``data/tests/universe/test_runtime.py`` already use for migration 009's
   tables, and the fixture-backed CRUD/query round-trips in
   ``test_eligibility_runtime.py`` further exercise every column and
   constraint this migration adds.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from sqlalchemy import create_engine, inspect

from data.universe.models import Base


def _migration_module():
    return importlib.import_module(
        "infra.db.migrations.versions."
        "013_universe_eligibility_attributes"
    )


def test_revision_chain_follows_012() -> None:
    mod = _migration_module()
    assert mod.revision == "013"
    assert mod.down_revision == "012"


def test_upgrade_and_downgrade_functions_exist() -> None:
    mod = _migration_module()
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_no_other_migration_claims_revision_013() -> None:
    """Guards against a duplicate revision id the way migrations 004a/004b
    (strategy_registry / trade_journal_schema) show CAN happen in this
    repo's history -- 013 must be unique among the versions directory."""
    versions_dir = (
        Path(__file__).resolve().parents[3] / "infra" / "db" / "migrations" / "versions"
    )
    claimants = []
    for path in sorted(versions_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("revision") and '"013"' in stripped:
                claimants.append(path.name)
                break
    assert claimants == ["013_universe_eligibility_attributes.py"], claimants


def test_upgrade_equivalent_orm_models_create_and_drop_cleanly(tmp_path) -> None:
    """"Upgrade"/"downgrade" shape check via the mirrored ORM models
    (project convention -- see module docstring): both new tables and every
    declared constraint/index create without error and drop cleanly,
    leaving no residue, for the full membership+eligibility schema together
    (proving the eligibility tables' FK to universe_eligibility_batches and
    coexistence with migration 009's tables is sound)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'migration_013.db'}", future=True)

    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "universe_eligibility_batches" in table_names
    assert "universe_eligibility_attributes" in table_names

    attr_cols = {c["name"] for c in inspector.get_columns("universe_eligibility_attributes")}
    assert attr_cols == {
        "id",
        "universe_id",
        "ticker",
        "attribute_name",
        "attribute_value_numeric",
        "attribute_value_text",
        "effective_start",
        "effective_end",
        "computed_from",
        "source_data_asof",
        "computation_batch_id",
        "created_at",
    }
    batch_cols = {c["name"] for c in inspector.get_columns("universe_eligibility_batches")}
    assert batch_cols == {
        "id",
        "universe_id",
        "code_version",
        "computed_at",
        "n_attribute_rows",
        "notes",
        "created_at",
    }

    fks = inspector.get_foreign_keys("universe_eligibility_attributes")
    assert any(
        fk["referred_table"] == "universe_eligibility_batches"
        and fk["constrained_columns"] == ["computation_batch_id"]
        for fk in fks
    )

    Base.metadata.drop_all(engine)
    inspector = inspect(engine)
    remaining = set(inspector.get_table_names())
    assert "universe_eligibility_batches" not in remaining
    assert "universe_eligibility_attributes" not in remaining

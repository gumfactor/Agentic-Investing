"""Tests for reporting.dashboards.queries — query functions against a test DB.

Uses an in-memory SQLite database seeded with known data.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from reporting.dashboards.queries import (
    blotter_approval_history,
    insert_blotter_approval,
    pending_blotter,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    """Create an in-memory SQLite database with the blotter_approvals table."""
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE blotter_approvals (
                id TEXT PRIMARY KEY,
                blotter_run_id TEXT NOT NULL UNIQUE,
                blotter_local_path TEXT NOT NULL,
                blotter_sha256 TEXT NOT NULL,
                selected_order_ids TEXT NOT NULL,
                approved_at_utc TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                approved_by TEXT NOT NULL,
                confirmed_blotter_sha256 TEXT NOT NULL,
                dashboard_session_id TEXT,
                quantity_overrides TEXT,
                notes TEXT
            )
        """))
    return eng


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


def _rows_checksum(rows: list[dict]) -> str:
    """Match the production checksum logic in paper_stage_blotter_check."""
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_blotter(artifact_dir: Path, run_id: str, age_hours: float = 0) -> Path:
    """Write a minimal blotter JSON artifact and return the path."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_rows = [
        {"sequence": 1, "ticker": "AAPL", "side": "BUY", "quantity": 10,
         "reference_price": 200.0, "estimated_notional": 2000.0},
    ]
    blotter = {
        "run_id": run_id,
        "generated_at_utc": "2026-06-29T23:00:00Z",
        "paper_only": True,
        "stage_only": True,
        "strategy_id": "v1_base_momentum",
        "candidate_rows": candidate_rows,
        "candidate_rows_sha256": _rows_checksum(candidate_rows),
    }
    path = artifact_dir / f"blotter_{run_id}.json"
    with open(path, "w") as f:
        json.dump(blotter, f)

    if age_hours > 0:
        import time
        target_time = time.time() - (age_hours * 3600)
        os.utime(path, (target_time, target_time))

    return path


# ── pending_blotter tests ─────────────────────────────────────────────────────

class TestPendingBlotter:
    def test_no_artifact_dir(self, engine):
        result = pending_blotter(Path("/nonexistent"), engine)
        assert result is None

    def test_empty_dir(self, engine, artifact_dir: Path):
        artifact_dir.mkdir(parents=True)
        result = pending_blotter(artifact_dir, engine)
        assert result is None

    def test_finds_pending_blotter(self, engine, artifact_dir: Path):
        path = _write_blotter(artifact_dir, "run-001")
        result = pending_blotter(artifact_dir, engine)
        assert result is not None
        assert result["blotter"]["run_id"] == "run-001"
        assert result["path"] == path

    def test_skips_already_approved(self, engine, artifact_dir: Path):
        _write_blotter(artifact_dir, "run-002")
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO blotter_approvals
                    (id, blotter_run_id, blotter_local_path, blotter_sha256,
                     selected_order_ids, approved_by, confirmed_blotter_sha256)
                VALUES
                    (:id, 'run-002', '/tmp/x.json', :sha, '["1"]', 'op@test.com', :sha)
            """), {"id": str(uuid.uuid4()), "sha": "a" * 64})

        result = pending_blotter(artifact_dir, engine)
        assert result is None

    def test_skips_stale_artifact(self, engine, artifact_dir: Path):
        _write_blotter(artifact_dir, "run-003", age_hours=48)
        result = pending_blotter(artifact_dir, engine)
        assert result is None

    def test_skips_invalid_json(self, engine, artifact_dir: Path):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        bad_path = artifact_dir / "blotter_bad.json"
        bad_path.write_text("not json{{{")
        result = pending_blotter(artifact_dir, engine)
        assert result is None

    def test_skips_missing_run_id(self, engine, artifact_dir: Path):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / "blotter_no_id.json"
        path.write_text(json.dumps({"candidate_rows": []}))
        result = pending_blotter(artifact_dir, engine)
        assert result is None

    def test_returns_most_recent_pending(self, engine, artifact_dir: Path):
        import time
        _write_blotter(artifact_dir, "run-old")
        time.sleep(0.1)
        _write_blotter(artifact_dir, "run-new")
        result = pending_blotter(artifact_dir, engine)
        assert result is not None
        assert result["blotter"]["run_id"] == "run-new"


# ── insert_blotter_approval tests ─────────────────────────────────────────────

class TestInsertBlotterApproval:
    def test_successful_insert(self, engine):
        insert_blotter_approval(
            engine,
            run_id="run-100",
            local_path="/tmp/blotter.json",
            blotter_sha256="b" * 64,
            selected_ids=["1", "2", "3"],
            approved_by="test@test.com",
            confirmed_hash="b" * 64,
            session_id="session-abc",
            quantity_overrides=None,
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM blotter_approvals WHERE blotter_run_id = 'run-100'")
            ).fetchone()
        assert row is not None

    def test_double_approval_raises(self, engine):
        kwargs = dict(
            run_id="run-200",
            local_path="/tmp/blotter.json",
            blotter_sha256="c" * 64,
            selected_ids=["1"],
            approved_by="test@test.com",
            confirmed_hash="c" * 64,
            session_id="session-def",
            quantity_overrides=None,
        )
        insert_blotter_approval(engine, **kwargs)
        with pytest.raises(IntegrityError):
            insert_blotter_approval(engine, **kwargs)

    def test_quantity_overrides_stored(self, engine):
        overrides = {"1": 5, "3": 7}
        insert_blotter_approval(
            engine,
            run_id="run-300",
            local_path="/tmp/blotter.json",
            blotter_sha256="d" * 64,
            selected_ids=["1", "3"],
            approved_by="test@test.com",
            confirmed_hash="d" * 64,
            session_id="session-ghi",
            quantity_overrides=overrides,
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT quantity_overrides FROM blotter_approvals WHERE blotter_run_id = 'run-300'")
            ).fetchone()
        stored = json.loads(row[0]) if row[0] else {}
        assert stored == {"1": 5, "3": 7}


# ── blotter_approval_history tests ────────────────────────────────────────────

class TestBlotterApprovalHistory:
    def test_empty_table(self, engine):
        df = blotter_approval_history(engine, limit=10)
        assert len(df) == 0

    def test_returns_rows(self, engine):
        insert_blotter_approval(
            engine,
            run_id="run-400",
            local_path="/tmp/blotter.json",
            blotter_sha256="e" * 64,
            selected_ids=["1"],
            approved_by="test@test.com",
            confirmed_hash="e" * 64,
            session_id="session-jkl",
            quantity_overrides=None,
        )
        df = blotter_approval_history(engine, limit=10)
        assert len(df) == 1
        assert df.iloc[0]["blotter_run_id"] == "run-400"

    def test_respects_limit(self, engine):
        for i in range(5):
            insert_blotter_approval(
                engine,
                run_id=f"run-5{i:02d}",
                local_path="/tmp/blotter.json",
                blotter_sha256="f" * 64,
                selected_ids=["1"],
                approved_by="test@test.com",
                confirmed_hash="f" * 64,
                session_id=f"session-{i}",
                quantity_overrides=None,
            )
        df = blotter_approval_history(engine, limit=3)
        assert len(df) == 3

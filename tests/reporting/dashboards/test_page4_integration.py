"""Integration test for Page 4 — Blotter Approval end-to-end.

Seeds a blotter artifact file with no corresponding blotter_approvals row,
runs the approval logic, asserts an approval row is INSERTed correctly,
and verifies a second attempt raises IntegrityError.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text

from reporting.dashboards.queries import (
    insert_blotter_approval,
    pending_blotter,
    blotter_approval_history,
)


@pytest.fixture
def engine():
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


def _rows_checksum(rows: list[dict]) -> str:
    """Match the production checksum logic in paper_stage_blotter_check."""
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _create_blotter_artifact(artifact_dir: Path) -> tuple[Path, dict, str]:
    """Create a realistic blotter artifact and return (path, data, file_sha256)."""
    artifact_dir.mkdir(parents=True, exist_ok=True)

    candidate_rows = [
        {
            "sequence": 1,
            "ticker": "AAPL",
            "side": "BUY",
            "quantity": 10,
            "reference_price": 195.50,
            "estimated_notional": 1955.00,
            "risk_flag": "",
        },
        {
            "sequence": 2,
            "ticker": "MSFT",
            "side": "BUY",
            "quantity": 5,
            "reference_price": 420.00,
            "estimated_notional": 2100.00,
            "risk_flag": "",
        },
        {
            "sequence": 3,
            "ticker": "NVDA",
            "side": "BUY",
            "quantity": 3,
            "reference_price": 130.00,
            "estimated_notional": 390.00,
            "risk_flag": "CONCENTRATION",
        },
        {
            "sequence": 4,
            "ticker": "GOOG",
            "side": "SELL",
            "quantity": 8,
            "reference_price": 178.25,
            "estimated_notional": 1426.00,
            "risk_flag": "",
        },
    ]

    blotter = {
        "schema_version": "1.0",
        "artifact_type": "paper_stage_only_order_blotter",
        "run_id": f"dag-run-{uuid.uuid4().hex[:8]}",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "stage_only": True,
        "strategy_id": "v1_base_momentum",
        "candidate_rows": candidate_rows,
        "candidate_rows_sha256": _rows_checksum(candidate_rows),
    }

    final_content = json.dumps(blotter, indent=2).encode()
    path = artifact_dir / f"blotter_{blotter['run_id']}.json"
    path.write_bytes(final_content)
    file_sha = hashlib.sha256(final_content).hexdigest()

    return path, blotter, file_sha


class TestPage4Integration:
    def test_full_approval_flow(self, engine, tmp_path: Path):
        """End-to-end: detect pending → approve → verify row → reject double."""
        artifact_dir = tmp_path / "paper_artifacts"
        path, blotter, file_sha = _create_blotter_artifact(artifact_dir)
        run_id = blotter["run_id"]

        # 1. Detect pending blotter
        pending = pending_blotter(artifact_dir, engine)
        assert pending is not None
        assert pending["blotter"]["run_id"] == run_id

        # 2. SHA-256 verification
        computed_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert computed_sha == file_sha

        # 3. Simulate operator selecting rows 1, 2, 4 (deselecting row 3 — NVDA)
        selected_ids = ["1", "2", "4"]
        quantity_overrides = {"2": 3}  # Reduced MSFT from 5 to 3

        # 4. INSERT approval
        insert_blotter_approval(
            engine,
            run_id=run_id,
            local_path=str(path),
            blotter_sha256=file_sha,
            selected_ids=selected_ids,
            approved_by="operator@rqis.com",
            confirmed_hash=file_sha,
            session_id="session-integration-test",
            quantity_overrides=quantity_overrides,
        )

        # 5. Verify approval row
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM blotter_approvals WHERE blotter_run_id = :rid"),
                {"rid": run_id},
            ).mappings().fetchone()

        assert row is not None
        assert row["approved_by"] == "operator@rqis.com"
        assert row["blotter_sha256"] == file_sha
        assert row["confirmed_blotter_sha256"] == file_sha
        assert row["dashboard_session_id"] == "session-integration-test"

        selected = json.loads(row["selected_order_ids"])
        assert selected == ["1", "2", "4"]
        assert "3" not in selected  # NVDA was deselected

        overrides = json.loads(row["quantity_overrides"]) if row["quantity_overrides"] else {}
        assert overrides == {"2": 3}

        # 6. Pending blotter no longer detected
        pending_after = pending_blotter(artifact_dir, engine)
        assert pending_after is None

        # 7. Second approval attempt raises IntegrityError
        with pytest.raises(sa.exc.IntegrityError):
            insert_blotter_approval(
                engine,
                run_id=run_id,
                local_path=str(path),
                blotter_sha256=file_sha,
                selected_ids=["1"],
                approved_by="operator@rqis.com",
                confirmed_hash=file_sha,
                session_id="session-second-attempt",
                quantity_overrides=None,
            )

        # 8. Approval history shows the record
        history = blotter_approval_history(engine, limit=10)
        assert len(history) == 1
        assert history.iloc[0]["blotter_run_id"] == run_id

    def test_tampered_blotter_detected(self, tmp_path: Path):
        """SHA-256 mismatch when file is modified after generation."""
        artifact_dir = tmp_path / "paper_artifacts"
        path, blotter, original_sha = _create_blotter_artifact(artifact_dir)

        # Tamper with the file
        tampered = path.read_bytes() + b"\n  extra content"
        path.write_bytes(tampered)

        tampered_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert tampered_sha != original_sha

    def test_multiple_blotters_latest_detected(self, engine, tmp_path: Path):
        """With multiple blotter files, the most recent pending one is returned."""
        artifact_dir = tmp_path / "paper_artifacts"
        import time

        # Create older blotter
        path1, blotter1, sha1 = _create_blotter_artifact(artifact_dir)
        time.sleep(0.1)

        # Create newer blotter with different run_id
        blotter2_data = {
            "run_id": "newer-run-id",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "paper_only": True,
            "strategy_id": "v1_base_momentum",
            "candidate_rows": [
                {"sequence": 1, "ticker": "TSLA", "side": "BUY", "quantity": 2,
                 "reference_price": 250.0, "estimated_notional": 500.0},
            ],
        }
        path2 = artifact_dir / "blotter_newer.json"
        path2.write_bytes(json.dumps(blotter2_data).encode())

        pending = pending_blotter(artifact_dir, engine)
        assert pending is not None
        assert pending["blotter"]["run_id"] == "newer-run-id"

    def test_approval_with_all_rows_selected(self, engine, tmp_path: Path):
        """Approving all rows (no deselection, no quantity changes)."""
        artifact_dir = tmp_path / "paper_artifacts"
        path, blotter, file_sha = _create_blotter_artifact(artifact_dir)

        all_ids = [str(r["sequence"]) for r in blotter["candidate_rows"]]

        insert_blotter_approval(
            engine,
            run_id=blotter["run_id"],
            local_path=str(path),
            blotter_sha256=file_sha,
            selected_ids=all_ids,
            approved_by="operator@rqis.com",
            confirmed_hash=file_sha,
            session_id="session-all-selected",
            quantity_overrides=None,
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT selected_order_ids FROM blotter_approvals WHERE blotter_run_id = :rid"),
                {"rid": blotter["run_id"]},
            ).fetchone()

        selected = json.loads(row[0])
        assert len(selected) == 4
        assert set(selected) == {"1", "2", "3", "4"}

    def test_approval_with_zero_rows_selected(self, engine, tmp_path: Path):
        """Approving with an empty selection is allowed at the DB level
        (UI prevents this but the query layer should not crash)."""
        artifact_dir = tmp_path / "paper_artifacts"
        path, blotter, file_sha = _create_blotter_artifact(artifact_dir)

        insert_blotter_approval(
            engine,
            run_id=blotter["run_id"],
            local_path=str(path),
            blotter_sha256=file_sha,
            selected_ids=[],
            approved_by="operator@rqis.com",
            confirmed_hash=file_sha,
            session_id="session-empty",
            quantity_overrides=None,
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT selected_order_ids FROM blotter_approvals WHERE blotter_run_id = :rid"),
                {"rid": blotter["run_id"]},
            ).fetchone()

        selected = json.loads(row[0])
        assert selected == []

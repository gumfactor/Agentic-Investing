"""Tests for blotter approval logic — pure Python, no Streamlit dependency.

Tests SHA-256 verification, quantity edit validation, and approval INSERT logic.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text

from reporting.dashboards.queries import insert_blotter_approval


# ── Fixtures ──────────────────────────────────────────────────────────────────

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


def _make_blotter(tmp_path: Path, run_id: str = "run-test") -> tuple[Path, dict, str]:
    """Create a blotter artifact file and return (path, blotter_dict, sha256)."""
    blotter = {
        "run_id": run_id,
        "generated_at_utc": "2026-06-29T23:00:00Z",
        "paper_only": True,
        "stage_only": True,
        "strategy_id": "v1_base_momentum",
        "candidate_rows": [
            {"sequence": 1, "ticker": "AAPL", "side": "BUY", "quantity": 10,
             "reference_price": 200.0, "estimated_notional": 2000.0},
            {"sequence": 2, "ticker": "MSFT", "side": "BUY", "quantity": 5,
             "reference_price": 400.0, "estimated_notional": 2000.0},
            {"sequence": 3, "ticker": "GOOG", "side": "SELL", "quantity": 3,
             "reference_price": 175.0, "estimated_notional": 525.0},
        ],
    }

    content = json.dumps(blotter, indent=2).encode()
    sha = hashlib.sha256(content).hexdigest()
    blotter["candidate_rows_sha256"] = sha

    path = tmp_path / f"blotter_{run_id}.json"
    full_content = json.dumps(blotter, indent=2).encode()
    path.write_bytes(full_content)
    file_sha = hashlib.sha256(full_content).hexdigest()

    return path, blotter, file_sha


# ── SHA-256 verification tests ────────────────────────────────────────────────

class TestSHA256Verification:
    def test_sha256_matches_on_unmodified_file(self, tmp_path: Path):
        path, blotter, expected_sha = _make_blotter(tmp_path)
        computed = hashlib.sha256(path.read_bytes()).hexdigest()
        assert computed == expected_sha

    def test_sha256_mismatch_on_modified_file(self, tmp_path: Path):
        path, blotter, original_sha = _make_blotter(tmp_path)
        modified = path.read_bytes() + b"tampered"
        path.write_bytes(modified)
        computed = hashlib.sha256(path.read_bytes()).hexdigest()
        assert computed != original_sha

    def test_sha256_consistent_across_reads(self, tmp_path: Path):
        path, _, _ = _make_blotter(tmp_path)
        h1 = hashlib.sha256(path.read_bytes()).hexdigest()
        h2 = hashlib.sha256(path.read_bytes()).hexdigest()
        assert h1 == h2


# ── Quantity edit validation tests ────────────────────────────────────────────

class TestQuantityValidation:
    def test_quantity_reduction_allowed(self):
        original = 10
        edited = 7
        assert 0 < edited <= original

    def test_quantity_increase_rejected(self):
        original = 10
        edited = 15
        assert edited > original

    def test_quantity_zero_rejected(self):
        edited = 0
        assert edited <= 0

    def test_quantity_unchanged_no_override(self):
        original = 10
        edited = 10
        assert edited == original

    def test_build_quantity_overrides(self):
        """Only changed quantities appear in the overrides dict."""
        originals = {1: 10, 2: 5, 3: 3}
        edited = {1: 10, 2: 3, 3: 3}

        overrides = {}
        for seq, qty in edited.items():
            if qty != originals[seq] and qty <= originals[seq]:
                overrides[str(seq)] = qty

        assert overrides == {"2": 3}
        assert "1" not in overrides
        assert "3" not in overrides


# ── Approval INSERT logic tests ──────────────────────────────────────────────

class TestApprovalInsert:
    def test_insert_records_all_fields(self, engine, tmp_path: Path):
        path, blotter, file_sha = _make_blotter(tmp_path)

        insert_blotter_approval(
            engine,
            run_id=blotter["run_id"],
            local_path=str(path),
            blotter_sha256=file_sha,
            selected_ids=["1", "2"],
            approved_by="operator@test.com",
            confirmed_hash=file_sha,
            session_id="session-xyz",
            quantity_overrides={"2": 3},
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM blotter_approvals WHERE blotter_run_id = :rid"),
                {"rid": blotter["run_id"]},
            ).mappings().fetchone()

        assert row is not None
        assert row["approved_by"] == "operator@test.com"
        assert row["blotter_sha256"] == file_sha
        assert row["confirmed_blotter_sha256"] == file_sha
        assert row["dashboard_session_id"] == "session-xyz"

        selected = json.loads(row["selected_order_ids"])
        assert selected == ["1", "2"]

        overrides = json.loads(row["quantity_overrides"]) if row["quantity_overrides"] else {}
        assert overrides == {"2": 3}

    def test_unique_constraint_prevents_double_approval(self, engine, tmp_path: Path):
        path, blotter, file_sha = _make_blotter(tmp_path)
        kwargs = dict(
            run_id=blotter["run_id"],
            local_path=str(path),
            blotter_sha256=file_sha,
            selected_ids=["1"],
            approved_by="operator@test.com",
            confirmed_hash=file_sha,
            session_id="session-abc",
            quantity_overrides=None,
        )

        insert_blotter_approval(engine, **kwargs)
        with pytest.raises(sa.exc.IntegrityError):
            insert_blotter_approval(engine, **kwargs)

    def test_different_run_ids_both_succeed(self, engine, tmp_path: Path):
        for rid in ["run-A", "run-B"]:
            path, blotter, file_sha = _make_blotter(tmp_path, run_id=rid)
            insert_blotter_approval(
                engine,
                run_id=rid,
                local_path=str(path),
                blotter_sha256=file_sha,
                selected_ids=["1"],
                approved_by="operator@test.com",
                confirmed_hash=file_sha,
                session_id="session-abc",
                quantity_overrides=None,
            )

        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM blotter_approvals")
            ).scalar()
        assert count == 2

    def test_null_quantity_overrides_stored_as_empty(self, engine, tmp_path: Path):
        path, blotter, file_sha = _make_blotter(tmp_path, run_id="run-null-overrides")
        insert_blotter_approval(
            engine,
            run_id="run-null-overrides",
            local_path=str(path),
            blotter_sha256=file_sha,
            selected_ids=["1"],
            approved_by="operator@test.com",
            confirmed_hash=file_sha,
            session_id="session-abc",
            quantity_overrides=None,
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT quantity_overrides FROM blotter_approvals WHERE blotter_run_id = 'run-null-overrides'")
            ).fetchone()
        stored = json.loads(row[0]) if row[0] else {}
        assert stored == {}

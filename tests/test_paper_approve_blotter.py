"""Tests for the paper_approve_blotter CLI approval command."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts import paper_approve_blotter as approve


def _write_blotter(tmp_path: Path, *, run_id: str = "run-xyz", n_rows: int = 2) -> Path:
    rows = [
        {
            "sequence": i + 1,
            "ticker": f"TICK{i}",
            "direction": "BUY",
            "review_status": "LOCAL_STAGE_ONLY",
            "current_weight": 0.0,
            "target_weight": 0.02,
            "delta_weight": 0.02,
            "reference_price": 100.0,
            "estimated_shares": 2.0,
            "estimated_notional": 200.0,
        }
        for i in range(n_rows)
    ]
    artifact: dict[str, Any] = {
        "schema_version": "paper_stage_blotter.v1",
        "artifact_type": "paper_stage_only_order_blotter",
        "run_id": run_id,
        "generated_at_utc": "2026-06-25T23:06:00+00:00",
        "paper_only": True,
        "stage_only": True,
        "strategy_id": "v1_base_momentum",
        "trading_date": "2026-06-25",
        "candidate_rows": rows,
    }
    path = tmp_path / "blotter.json"
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return path


class TestDryRun:
    def test_dry_run_succeeds_without_db(self, tmp_path):
        path = _write_blotter(tmp_path)
        rc = approve.run(argv=["--blotter", str(path), "--dry-run"])
        assert rc == 0

    def test_dry_run_does_not_call_db(self, tmp_path):
        path = _write_blotter(tmp_path)
        with patch("scripts.paper_approve_blotter.create_engine") as mock_eng:
            approve.run(argv=["--blotter", str(path), "--dry-run"])
        mock_eng.assert_not_called()

    def test_missing_blotter_file_returns_1(self, tmp_path):
        rc = approve.run(argv=["--blotter", str(tmp_path / "nope.json"), "--dry-run"])
        assert rc == 1


class TestSchemaValidation:
    def test_rejects_wrong_schema_version(self, tmp_path):
        path = _write_blotter(tmp_path)
        artifact = json.loads(path.read_text())
        artifact["schema_version"] = "wrong.v1"
        path.write_text(json.dumps(artifact))
        rc = approve.run(argv=["--blotter", str(path), "--dry-run"])
        assert rc == 1

    def test_rejects_live_blotter(self, tmp_path):
        path = _write_blotter(tmp_path)
        artifact = json.loads(path.read_text())
        artifact["paper_only"] = False
        path.write_text(json.dumps(artifact))
        rc = approve.run(argv=["--blotter", str(path), "--dry-run"])
        assert rc == 1


class TestResolveSelectedIds:
    def test_all_returns_all_sequences(self):
        rows = [{"sequence": 1}, {"sequence": 2}, {"sequence": 3}]
        assert approve._resolve_selected_ids("ALL", rows) == [1, 2, 3]

    def test_subset_returns_sorted_unique(self):
        rows = [{"sequence": 1}, {"sequence": 2}, {"sequence": 3}]
        assert approve._resolve_selected_ids("3,1", rows) == [1, 3]

    def test_unknown_sequence_raises(self):
        rows = [{"sequence": 1}, {"sequence": 2}]
        with pytest.raises(SystemExit, match="sequence 99 not found"):
            approve._resolve_selected_ids("99", rows)

    def test_non_integer_raises(self):
        rows = [{"sequence": 1}]
        with pytest.raises(SystemExit, match="non-integer"):
            approve._resolve_selected_ids("abc", rows)


class TestApprovalInsertion:
    def test_happy_path_inserts_row(self, tmp_path, monkeypatch):
        path = _write_blotter(tmp_path, run_id="test-run-1")
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        with patch("scripts.paper_approve_blotter.create_engine", return_value=mock_engine):
            with patch("scripts.paper_approve_blotter.input", return_value="YES"):
                rc = approve.run(
                    argv=[
                        "--blotter", str(path),
                        "--operator", "tester@example.com",
                    ]
                )

        assert rc == 0
        mock_conn.execute.assert_called_once()
        call_kwargs = mock_conn.execute.call_args[0][1]
        assert call_kwargs["run_id"] == "test-run-1"
        assert call_kwargs["approved_by"] == "tester@example.com"
        assert call_kwargs["sha256"] == call_kwargs["confirmed_sha256"]

    def test_non_yes_input_returns_1(self, tmp_path, monkeypatch):
        path = _write_blotter(tmp_path)
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        with patch("scripts.paper_approve_blotter.input", return_value="no"):
            rc = approve.run(argv=["--blotter", str(path)])
        assert rc == 1

    def test_missing_database_url_returns_1(self, tmp_path, monkeypatch):
        path = _write_blotter(tmp_path)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with patch("scripts.paper_approve_blotter.input", return_value="YES"):
            rc = approve.run(argv=["--blotter", str(path)])
        assert rc == 1

    def test_subset_order_ids(self, tmp_path, monkeypatch):
        path = _write_blotter(tmp_path, n_rows=3)
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        with patch("scripts.paper_approve_blotter.create_engine", return_value=mock_engine):
            with patch("scripts.paper_approve_blotter.input", return_value="YES"):
                rc = approve.run(
                    argv=["--blotter", str(path), "--order-ids", "1,3"]
                )

        assert rc == 0
        call_kwargs = mock_conn.execute.call_args[0][1]
        selected = json.loads(call_kwargs["selected_ids"])
        assert selected == [1, 3]

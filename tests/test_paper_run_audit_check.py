"""Tests for the Step 8 paper run audit record command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import paper_run_audit_check as check
from scripts import paper_stage_blotter_check as stage
from scripts import paper_submit_reconcile_check as submit


def _write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _write_blotter(tmp_path: Path, *, mutate: Any = None) -> Path:
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    blotter_path = tmp_path / "paper_stage_blotter.json"
    _write_file(config_path, "version: 1\nname: base_momentum\n")
    _write_file(portfolio_path, json.dumps({"as_of": "2026-06-20", "cash": 1000.0, "positions": []}))
    rows = [
        {
            "sequence": 1,
            "ticker": "AAPL",
            "direction": "BUY",
            "review_status": "LOCAL_STAGE_ONLY",
            "current_weight": 0.0,
            "target_weight": 0.5,
            "delta_weight": 0.5,
            "reference_price": 200.0,
            "estimated_shares": 2.5,
            "estimated_notional": 500.0,
        }
    ]
    artifact = {
        "schema_version": "paper_stage_blotter.v1",
        "artifact_type": "paper_stage_only_order_blotter",
        "run_id": "step-6-run",
        "generated_at_utc": "2026-06-20T14:30:00+00:00",
        "paper_only": True,
        "stage_only": True,
        "strategy_id": "v1_base_momentum",
        "strategy_config": str(config_path),
        "provenance": {
            "strategy_config_path": str(config_path),
            "strategy_config_sha256": stage._file_sha256(config_path),
            "portfolio_input_path": str(portfolio_path),
            "portfolio_input_sha256": stage._file_sha256(portfolio_path),
            "gate_inputs": {
                "target_as_of_date": "2026-06-19",
                "portfolio_snapshot_as_of": "2026-06-20",
                "max_position_weight": 0.6,
                "max_gross_target_weight": 1.0,
                "allow_shorts": False,
                "max_turnover_weight": None,
                "min_order_notional": 0.0,
            },
        },
        "source": {
            "step5_required": True,
            "target_as_of_date": "2026-06-19",
            "portfolio_snapshot_as_of": "2026-06-20",
        },
        "safety": {
            "broker_connected": False,
            "broker_order_ids_present": False,
            "order_manager_registered": False,
            "orders_submitted": False,
            "orders_cancelled": False,
            "fills_reconciled": False,
            "human_yes_consumed": False,
        },
        "risk_compliance_summary": {"candidate_count": 1},
        "candidate_rows_sha256": stage._rows_checksum(rows),
        "candidate_rows": rows,
        "output_path": str(blotter_path),
    }
    artifact["provenance"]["gate_inputs_sha256"] = stage._stable_sha256(
        artifact["provenance"]["gate_inputs"]
    )
    if mutate is not None:
        mutate(artifact)
    artifact["artifact_sha256"] = stage._artifact_checksum(artifact)
    blotter_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    return blotter_path


def _write_reconciliation(
    tmp_path: Path,
    blotter_path: Path,
    *,
    status: str = "SUBMITTED",
    mutate: Any = None,
) -> Path:
    blotter = json.loads(blotter_path.read_text(encoding="utf-8"))
    reconciliation_path = tmp_path / "paper_submit_reconciliation.json"
    broker_responses = [
        {
            "sequence": 1,
            "ticker": "AAPL",
            "direction": "BUY",
            "submitted_quantity": 2.5,
            "limit_price": 200.0,
            "broker_order_id": "paper-1",
            "submitted_at_utc": "2026-06-20T15:00:00+00:00",
            "initial_fill_poll": {"status": "Submitted"},
        }
    ]
    artifact = {
        "schema_version": "paper_submit_reconcile.v1",
        "artifact_type": "paper_submit_reconciliation",
        "run_id": "step-7-run",
        "generated_at_utc": "2026-06-20T15:00:00+00:00",
        "paper_only": True,
        "status": status,
        "live_port_supported": False,
        "source_blotter_path": str(blotter_path),
        "source_blotter_run_id": blotter["run_id"],
        "source_blotter_sha256": stage._file_sha256(blotter_path),
        "source_blotter_artifact_sha256": blotter["artifact_sha256"],
        "source_candidate_rows_sha256": blotter["candidate_rows_sha256"],
        "order_count": len(broker_responses),
        "last_attempted_sequence": None,
        "error": None,
        "broker_responses": broker_responses,
        "safety": {
            "operator_confirmed_yes": True,
            "paper_env_required": True,
            "ibkr_port": 7497,
            "orders_cancelled": False,
            "circuit_breaker_reset": False,
            "live_orders_allowed": False,
        },
    }
    if status == "FAILED":
        artifact["error"] = "simulated failure"
        artifact["last_attempted_sequence"] = 1
    if mutate is not None:
        mutate(artifact)
    artifact["artifact_sha256"] = submit._reconciliation_checksum(artifact)
    reconciliation_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    return reconciliation_path


def _git_metadata() -> dict[str, Any]:
    return {"branch": "local/linking-to-IBKR", "commit": "abc123", "dirty": True}


def test_writes_blocked_audit_record_from_blotter_and_readiness_note(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    readiness_path = tmp_path / "readiness.txt"
    readiness_path.write_text("manual readiness output", encoding="utf-8")
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--readiness-record",
            str(readiness_path),
            "--status",
            "BLOCKED",
            "--step1-status",
            "MANUAL",
            "--step2-status",
            "BLOCKED",
            "--blocker",
            "alpha_scores are stale for paper trading",
            "--output",
            str(output_path),
        ],
        now_fn=lambda: datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        run_id_factory=lambda: "step-8-run",
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper run audit record: OK" in out
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == "paper_run_audit.v1"
    assert artifact["artifact_type"] == "paper_run_audit_record"
    assert artifact["run_id"] == "step-8-run"
    assert artifact["generated_at_utc"] == "2026-06-20T16:00:00+00:00"
    assert artifact["paper_only"] is True
    assert artifact["operator_visible_status"] == "BLOCKED"
    assert artifact["inputs"]["gate_statuses"]["step1"] == "MANUAL"
    assert artifact["inputs"]["gate_statuses"]["step2"] == "BLOCKED"
    assert artifact["inputs"]["step6_blotter_run_id"] == "step-6-run"
    assert artifact["inputs"]["step7_reconciliation_run_id"] is None
    assert artifact["artifact_paths"]["blotter"]["sha256"] == stage._file_sha256(blotter_path)
    assert artifact["artifact_paths"]["readiness_record"]["sha256"] == stage._file_sha256(readiness_path)
    assert artifact["unresolved_blockers"] == ["alpha_scores are stale for paper trading"]
    assert artifact["git"] == _git_metadata()
    assert artifact["safety_assertions"] == {
        "broker_connected": False,
        "broker_orders_submitted": False,
        "broker_orders_cancelled": False,
        "broker_reconciliation_performed": False,
        "prior_artifacts_mutated": False,
        "circuit_breaker_reset_or_tripped": False,
        "human_yes_consumed": False,
        "live_orders_allowed": False,
    }
    assert artifact["artifact_sha256"] == check._audit_checksum(artifact)


def test_writes_submitted_record_with_valid_reconciliation(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    reconciliation_path = _write_reconciliation(tmp_path, blotter_path)
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--reconciliation",
            str(reconciliation_path),
            "--status",
            "SUBMITTED",
            "--step1-status",
            "PASS",
            "--step2-status",
            "PASS",
            "--step3-status",
            "PASS",
            "--step4-status",
            "PASS",
            "--step5-status",
            "PASS",
            "--output",
            str(output_path),
        ],
        now_fn=lambda: datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        run_id_factory=lambda: "step-8-submitted",
        git_metadata_factory=_git_metadata,
    )

    assert result == 0
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["operator_visible_status"] == "SUBMITTED"
    assert artifact["inputs"]["step7_reconciliation_run_id"] == "step-7-run"
    assert artifact["inputs"]["step7_status"] == "SUBMITTED"
    assert artifact["artifact_paths"]["reconciliation"]["sha256"] == stage._file_sha256(
        reconciliation_path
    )
    assert artifact["validation_summary"] == {
        "blotter_schema_valid": True,
        "blotter_checksums_valid": True,
        "reconciliation_schema_valid": True,
        "reconciliation_checksums_valid": True,
        "status_consistency_valid": True,
    }


def test_submitted_status_requires_reconciliation(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        ["--blotter", str(blotter_path), "--status", "SUBMITTED", "--output", str(output_path)],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Status SUBMITTED requires a Step 7 reconciliation artifact" in out
    assert not output_path.exists()


def test_dry_run_status_rejects_reconciliation(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    reconciliation_path = _write_reconciliation(tmp_path, blotter_path)
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--reconciliation",
            str(reconciliation_path),
            "--status",
            "DRY_RUN",
            "--output",
            str(output_path),
        ],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Status DRY_RUN must not include a reconciliation artifact" in out


def test_complete_status_rejects_unresolved_blockers(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    reconciliation_path = _write_reconciliation(tmp_path, blotter_path)
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--reconciliation",
            str(reconciliation_path),
            "--status",
            "COMPLETE",
            "--blocker",
            "alpha_scores stale",
            "--output",
            str(output_path),
        ],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Status COMPLETE cannot include unresolved blockers" in out


def test_failed_status_requires_failed_reconciliation_artifact(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        ["--blotter", str(blotter_path), "--status", "FAILED", "--output", str(output_path)],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Status FAILED requires a Step 7 failure reconciliation artifact" in out
    assert not output_path.exists()


def test_failed_status_rejects_submitted_reconciliation_artifact(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    reconciliation_path = _write_reconciliation(tmp_path, blotter_path, status="SUBMITTED")
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--reconciliation",
            str(reconciliation_path),
            "--status",
            "FAILED",
            "--output",
            str(output_path),
        ],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Status FAILED requires reconciliation status FAILED" in out
    assert not output_path.exists()


def test_rejects_reconciliation_checksum_mismatch(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    reconciliation_path = _write_reconciliation(tmp_path, blotter_path)
    artifact = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    artifact["source_candidate_rows_sha256"] = "0" * 64
    artifact["artifact_sha256"] = submit._reconciliation_checksum(artifact)
    reconciliation_path.write_text(json.dumps(artifact), encoding="utf-8")
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--reconciliation",
            str(reconciliation_path),
            "--status",
            "SUBMITTED",
            "--output",
            str(output_path),
        ],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "source_candidate_rows_sha256 does not match Step 6" in out
    assert not output_path.exists()


def test_rejects_reconciliation_with_live_port_safety_metadata(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)

    def mutate(artifact: dict[str, Any]) -> None:
        artifact["safety"]["ibkr_port"] = 7496

    reconciliation_path = _write_reconciliation(tmp_path, blotter_path, mutate=mutate)
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--reconciliation",
            str(reconciliation_path),
            "--status",
            "SUBMITTED",
            "--output",
            str(output_path),
        ],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Reconciliation safety.ibkr_port must be 7497" in out
    assert not output_path.exists()


def test_rejects_reconciliation_order_count_mismatch(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)

    def mutate(artifact: dict[str, Any]) -> None:
        artifact["order_count"] = 99

    reconciliation_path = _write_reconciliation(tmp_path, blotter_path, mutate=mutate)
    output_path = tmp_path / "paper_run_audit.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--reconciliation",
            str(reconciliation_path),
            "--status",
            "SUBMITTED",
            "--output",
            str(output_path),
        ],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Reconciliation order_count must match broker_responses length" in out
    assert not output_path.exists()


def test_refuses_to_overwrite_existing_artifact_without_flag(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_run_audit.json"
    output_path.write_text("do not replace", encoding="utf-8")

    result = check.run(
        ["--blotter", str(blotter_path), "--status", "BLOCKED", "--output", str(output_path)],
        git_metadata_factory=_git_metadata,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Pass --overwrite to replace it" in out
    assert output_path.read_text(encoding="utf-8") == "do not replace"


def test_write_artifact_non_overwrite_does_not_clobber_if_target_appears(tmp_path, monkeypatch):
    output_path = tmp_path / "paper_run_audit.json"
    artifact = {"schema_version": "test"}
    real_link = check.os.link

    def racing_link(src, dst):
        Path(dst).write_text("racing writer", encoding="utf-8")
        return real_link(src, dst)

    monkeypatch.setattr(check.os, "link", racing_link)

    try:
        check._write_artifact(output_path, artifact, overwrite=False)
    except RuntimeError as exc:
        assert "Pass --overwrite" in str(exc)
    else:
        raise AssertionError("Expected racing no-clobber write to fail")

    assert output_path.read_text(encoding="utf-8") == "racing writer"


def test_step_eight_does_not_import_or_call_broker_order_manager_or_yes_paths():
    source = Path(check.__file__).read_text(encoding="utf-8")

    assert "from execution.brokers" not in source
    assert "import execution.brokers" not in source
    assert "from execution.oms.order_manager" not in source
    assert "import execution.oms.order_manager" not in source
    assert "OrderManager(" not in source
    assert ".stage(" not in source
    assert ".submit_order(" not in source
    assert ".cancel" not in source
    assert "connect()" not in source

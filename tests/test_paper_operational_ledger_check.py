"""Tests for the daily paper operational ledger command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from reporting.audit import paper_operational_ledger as ledger
from scripts import paper_operational_ledger_check as check
from scripts import paper_order_reconcile_check as order_reconcile
from scripts import paper_run_audit_check as audit_check
from scripts import paper_stage_blotter_check as stage
from scripts import paper_submit_reconcile_check as submit
from tests.test_paper_run_audit_check import _git_metadata, _write_blotter, _write_reconciliation


def _write_submitted_audit(tmp_path: Path) -> tuple[Path, Path, Path]:
    blotter_path = _write_blotter(tmp_path)
    reconciliation_path = _write_reconciliation(tmp_path, blotter_path)
    audit_path = tmp_path / "paper_run_audit.json"
    result = audit_check.run(
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
            str(audit_path),
        ],
        now_fn=lambda: datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        run_id_factory=lambda: "step-8-run",
        git_metadata_factory=_git_metadata,
    )
    assert result == 0
    return blotter_path, reconciliation_path, audit_path


def _write_blocked_audit(tmp_path: Path) -> Path:
    blotter_path = _write_blotter(tmp_path)
    audit_path = tmp_path / "paper_run_audit_blocked.json"
    result = audit_check.run(
        [
            "--blotter",
            str(blotter_path),
            "--status",
            "BLOCKED",
            "--blocker",
            "alpha scores stale",
            "--output",
            str(audit_path),
        ],
        now_fn=lambda: datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        run_id_factory=lambda: "step-8-blocked",
        git_metadata_factory=_git_metadata,
    )
    assert result == 0
    return audit_path


def _write_order_reconciliation(tmp_path: Path, reconciliation_path: Path, *, status: str = "RECONCILED") -> Path:
    reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    path = tmp_path / "paper_order_reconciliation.json"
    results = [
        {
            "sequence": row["sequence"],
            "ticker": row["ticker"],
            "direction": row["direction"],
            "submitted_quantity": row["submitted_quantity"],
            "limit_price": row["limit_price"],
            "broker_order_id": row["broker_order_id"],
            "query_ok": True,
            "status_found": status == "RECONCILED",
            "broker_status": None
            if status != "RECONCILED"
            else {
                "broker_order_id": row["broker_order_id"],
                "status": "Filled",
                "filled_quantity": row["submitted_quantity"],
                "remaining_quantity": 0.0,
                "avg_price": row["limit_price"],
                "last_fill_price": row["limit_price"],
                "why_held": "",
            },
            "error": None,
        }
        for row in reconciliation["broker_responses"]
    ]
    artifact = {
        "schema_version": "paper_order_reconcile.v1",
        "artifact_type": "paper_order_reconciliation",
        "run_id": "order-reconcile-run",
        "generated_at_utc": "2026-06-21T14:00:00+00:00",
        "paper_only": True,
        "status": status,
        "source_reconciliation_path": str(reconciliation_path),
        "source_reconciliation_run_id": reconciliation["run_id"],
        "source_reconciliation_status": reconciliation["status"],
        "source_reconciliation_sha256": stage._file_sha256(reconciliation_path),
        "source_reconciliation_artifact_sha256": reconciliation["artifact_sha256"],
        "source_blotter_path": reconciliation["source_blotter_path"],
        "source_blotter_sha256": reconciliation["source_blotter_sha256"],
        "order_count": len(results),
        "status_found_count": sum(1 for row in results if row["status_found"]),
        "query_error_count": 0,
        "results": results,
        "safety": {
            "paper_env_required": True,
            "ibkr_port": 7497,
            "broker_connected_for_reconciliation": True,
            "orders_submitted": False,
            "orders_cancelled": False,
            "circuit_breaker_reset": False,
            "human_yes_consumed": False,
            "prior_artifacts_mutated": False,
            "live_orders_allowed": False,
        },
    }
    artifact["artifact_sha256"] = order_reconcile._artifact_checksum(artifact)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _rewrite_artifact(path: Path, artifact: dict, checksum_fn) -> None:
    artifact["artifact_sha256"] = checksum_fn(artifact)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_appends_submitted_daily_record_and_writes_report(tmp_path, capsys):
    _, reconciliation_path, audit_path = _write_submitted_audit(tmp_path)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"
    report_path = tmp_path / "paper_operational_report.json"

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "SUBMITTED",
            "--decision-reason",
            "tiny paper probe submitted; monitor fills next session",
            "--audit",
            str(audit_path),
            "--reconciliation",
            str(reconciliation_path),
            "--ledger",
            str(ledger_path),
            "--output-report",
            str(report_path),
            "--circuit-breaker-event",
            "no circuit breaker events observed",
            "--note",
            "APA/HAL/HPE tiny probe only",
        ],
        now_fn=lambda: datetime(2026, 6, 20, 18, 0, tzinfo=UTC),
        run_id_factory=lambda: "ledger-run",
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper operational ledger record: OK" in out
    records = _read_jsonl(ledger_path)
    assert len(records) == 1
    record = records[0]
    assert record["schema_version"] == "paper_operational_ledger.v1"
    assert record["artifact_type"] == "paper_daily_operational_record"
    assert record["run_id"] == "ledger-run"
    assert record["trading_date"] == "2026-06-20"
    assert record["decision"] == "SUBMITTED"
    assert record["summary"]["audit_status"] == "SUBMITTED"
    assert record["summary"]["step7_status"] == "SUBMITTED"
    assert record["summary"]["submitted_order_count"] == 1
    assert record["orders"] == [
        {
            "sequence": 1,
            "ticker": "AAPL",
            "direction": "BUY",
            "submitted_quantity": 2.5,
            "limit_price": 200.0,
            "broker_order_id": "paper-1",
            "submitted_at_utc": "2026-06-20T15:00:00+00:00",
            "initial_fill_poll": {"status": "Submitted"},
            "durable_status_found": None,
            "durable_query_ok": None,
            "durable_error": None,
            "broker_status": None,
        }
    ]
    assert record["fills"] == []
    assert record["artifact_paths"]["audit"]["sha256"] == stage._file_sha256(audit_path)
    assert record["artifact_paths"]["reconciliation"]["sha256"] == stage._file_sha256(
        reconciliation_path
    )
    assert record["circuit_breaker_events"] == ["no circuit breaker events observed"]
    assert record["notes"] == ["APA/HAL/HPE tiny probe only"]
    assert record["safety_assertions"]["broker_connected"] is False
    assert record["record_sha256"] == ledger._record_checksum(record)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "paper_operational_report.v1"
    assert report["ledger_record_sha256"] == record["record_sha256"]
    assert report["decision"] == "SUBMITTED"
    assert report["orders"] == record["orders"]
    assert report["fills"] == []


def test_complete_requires_clean_durable_reconciliation(tmp_path, capsys):
    _, reconciliation_path, audit_path = _write_submitted_audit(tmp_path)
    order_reconciliation_path = _write_order_reconciliation(tmp_path, reconciliation_path)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-21",
            "--decision",
            "COMPLETE",
            "--decision-reason",
            "all paper broker IDs reconciled",
            "--audit",
            str(audit_path),
            "--reconciliation",
            str(reconciliation_path),
            "--order-reconciliation",
            str(order_reconciliation_path),
            "--ledger",
            str(ledger_path),
        ],
        now_fn=lambda: datetime(2026, 6, 21, 15, 0, tzinfo=UTC),
        run_id_factory=lambda: "complete-ledger-run",
    )

    assert result == 0
    record = _read_jsonl(ledger_path)[0]
    assert record["decision"] == "COMPLETE"
    assert record["summary"]["durable_reconciliation_status"] == "RECONCILED"
    assert record["summary"]["durable_status_found_count"] == 1
    assert record["orders"][0]["durable_status_found"] is True
    assert record["orders"][0]["broker_status"]["status"] == "Filled"
    assert record["fills"] == [
        {
            "ticker": "AAPL",
            "direction": "BUY",
            "broker_order_id": "paper-1",
            "filled_quantity": 2.5,
            "avg_price": 200.0,
            "last_fill_price": 200.0,
            "broker_status": "Filled",
        }
    ]


def test_complete_rejects_unknown_durable_reconciliation(tmp_path, capsys):
    _, reconciliation_path, audit_path = _write_submitted_audit(tmp_path)
    order_reconciliation_path = _write_order_reconciliation(tmp_path, reconciliation_path)
    artifact = json.loads(order_reconciliation_path.read_text(encoding="utf-8"))
    artifact["status"] = "UNKNOWN"
    _rewrite_artifact(order_reconciliation_path, artifact, order_reconcile._artifact_checksum)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-21",
            "--decision",
            "COMPLETE",
            "--decision-reason",
            "operator marked complete",
            "--audit",
            str(audit_path),
            "--reconciliation",
            str(reconciliation_path),
            "--order-reconciliation",
            str(order_reconciliation_path),
            "--ledger",
            str(ledger_path),
        ],
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "requires durable order reconciliation status RECONCILED" in out
    assert not ledger_path.exists()


def test_complete_rejects_incomplete_durable_reconciliation_counts(tmp_path, capsys):
    _, reconciliation_path, audit_path = _write_submitted_audit(tmp_path)
    order_reconciliation_path = _write_order_reconciliation(tmp_path, reconciliation_path)
    artifact = json.loads(order_reconciliation_path.read_text(encoding="utf-8"))
    artifact["status_found_count"] = 0
    _rewrite_artifact(order_reconciliation_path, artifact, order_reconcile._artifact_checksum)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-21",
            "--decision",
            "COMPLETE",
            "--decision-reason",
            "operator marked complete",
            "--audit",
            str(audit_path),
            "--reconciliation",
            str(reconciliation_path),
            "--order-reconciliation",
            str(order_reconciliation_path),
            "--ledger",
            str(ledger_path),
        ],
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "status_found_count must equal order_count" in out
    assert not ledger_path.exists()


def test_rejects_unsafe_step7_safety_metadata_even_when_checksum_matches(tmp_path, capsys):
    _, reconciliation_path, _audit_path = _write_submitted_audit(tmp_path)
    reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    reconciliation["safety"]["orders_cancelled"] = True
    _rewrite_artifact(reconciliation_path, reconciliation, submit._reconciliation_checksum)
    blotter_path = Path(reconciliation["source_blotter_path"])
    audit_path = tmp_path / "paper_run_audit_unsafe.json"
    # Build a synthetic checksum-valid Step 8 reference so this test reaches the ledger safety gate.
    audit = {
        "schema_version": "paper_run_audit.v1",
        "artifact_type": "paper_run_audit_record",
        "run_id": "unsafe-step8",
        "generated_at_utc": "2026-06-20T16:00:00+00:00",
        "paper_only": True,
        "operator_visible_status": "SUBMITTED",
        "inputs": {"candidate_count": 1},
        "artifact_paths": {
            "blotter": {"path": str(blotter_path), "sha256": stage._file_sha256(blotter_path)},
            "reconciliation": {
                "path": str(reconciliation_path),
                "sha256": stage._file_sha256(reconciliation_path),
                "artifact_sha256": reconciliation["artifact_sha256"],
            },
        },
        "unresolved_blockers": [],
    }
    audit["artifact_sha256"] = audit_check._audit_checksum(audit)
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "SUBMITTED",
            "--decision-reason",
            "unsafe reconciliation should fail",
            "--audit",
            str(audit_path),
            "--reconciliation",
            str(reconciliation_path),
            "--ledger",
            str(ledger_path),
        ],
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Step 7 reconciliation safety.orders_cancelled must be False" in out
    assert not ledger_path.exists()


def test_blocked_record_does_not_require_submission_artifacts(tmp_path, capsys):
    audit_path = _write_blocked_audit(tmp_path)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "BLOCKED",
            "--decision-reason",
            "alpha scores stale",
            "--audit",
            str(audit_path),
            "--ledger",
            str(ledger_path),
        ],
        run_id_factory=lambda: "blocked-ledger-run",
    )

    assert result == 0
    record = _read_jsonl(ledger_path)[0]
    assert record["decision"] == "BLOCKED"
    assert record["summary"]["unresolved_blocker_count"] == 1
    assert "reconciliation" not in record["artifact_paths"]


def test_no_trade_rejects_submission_artifacts(tmp_path, capsys):
    _, reconciliation_path, audit_path = _write_submitted_audit(tmp_path)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "NO_TRADE",
            "--decision-reason",
            "should not include submitted orders",
            "--audit",
            str(audit_path),
            "--reconciliation",
            str(reconciliation_path),
            "--ledger",
            str(ledger_path),
        ],
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "NO_TRADE ledger decision requires a BLOCKED or DRY_RUN Step 8 audit" in out
    assert not ledger_path.exists()


def test_rejects_blank_decision_reason(tmp_path, capsys):
    audit_path = _write_blocked_audit(tmp_path)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "BLOCKED",
            "--decision-reason",
            "   ",
            "--audit",
            str(audit_path),
            "--ledger",
            str(ledger_path),
        ],
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "decision-reason must be non-empty" in out
    assert not ledger_path.exists()


def test_rejects_reconciliation_not_referenced_by_audit(tmp_path, capsys):
    run1 = tmp_path / "run1"
    run2 = tmp_path / "run2"
    run1.mkdir()
    run2.mkdir()
    _, reconciliation_path, _audit_path = _write_submitted_audit(run1)
    _other_blotter, _other_reconciliation, other_audit = _write_submitted_audit(run2)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "SUBMITTED",
            "--decision-reason",
            "mismatched artifacts",
            "--audit",
            str(other_audit),
            "--reconciliation",
            str(reconciliation_path),
            "--ledger",
            str(ledger_path),
        ],
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "does not match supplied Step 7 artifact" in out
    assert not ledger_path.exists()


def test_report_no_clobber_happens_before_ledger_append(tmp_path, capsys):
    _, reconciliation_path, audit_path = _write_submitted_audit(tmp_path)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"
    report_path = tmp_path / "paper_operational_report.json"
    report_path.write_text("existing", encoding="utf-8")

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "SUBMITTED",
            "--decision-reason",
            "tiny probe submitted",
            "--audit",
            str(audit_path),
            "--reconciliation",
            str(reconciliation_path),
            "--ledger",
            str(ledger_path),
            "--output-report",
            str(report_path),
        ],
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Pass --overwrite-report" in out
    assert report_path.read_text(encoding="utf-8") == "existing"
    assert not ledger_path.exists()


def test_report_parent_error_happens_before_ledger_append(tmp_path, capsys):
    _, reconciliation_path, audit_path = _write_submitted_audit(tmp_path)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"
    blocked_parent = tmp_path / "not_a_directory"
    blocked_parent.write_text("file blocks directory creation", encoding="utf-8")
    report_path = blocked_parent / "paper_operational_report.json"

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "SUBMITTED",
            "--decision-reason",
            "tiny probe submitted",
            "--audit",
            str(audit_path),
            "--reconciliation",
            str(reconciliation_path),
            "--ledger",
            str(ledger_path),
            "--output-report",
            str(report_path),
        ],
    )

    assert result == 1
    assert not ledger_path.exists()


def test_rejects_empty_decision_reason_before_append(tmp_path, capsys):
    audit_path = _write_blocked_audit(tmp_path)
    ledger_path = tmp_path / "paper_operational_ledger.jsonl"

    result = check.run(
        [
            "--trading-date",
            "2026-06-20",
            "--decision",
            "BLOCKED",
            "--decision-reason",
            "   ",
            "--audit",
            str(audit_path),
            "--ledger",
            str(ledger_path),
        ],
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "--decision-reason must be non-empty" in out
    assert not ledger_path.exists()

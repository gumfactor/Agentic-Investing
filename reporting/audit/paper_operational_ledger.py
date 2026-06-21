"""Append-only local ledger for daily paper-trading operational records."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from scripts.paper_order_reconcile_check import (
    ORDER_RECONCILE_SCHEMA_VERSION,
)
from scripts.paper_order_reconcile_check import (
    _artifact_checksum as _order_reconcile_checksum,
)
from scripts.paper_run_audit_check import AUDIT_SCHEMA_VERSION, _audit_checksum
from scripts.paper_stage_blotter_check import _file_sha256
from scripts.paper_submit_reconcile_check import (
    RECONCILIATION_SCHEMA_VERSION,
    _reconciliation_checksum,
)

LEDGER_SCHEMA_VERSION = "paper_operational_ledger.v1"
LEDGER_RECORD_TYPE = "paper_daily_operational_record"
REPORT_SCHEMA_VERSION = "paper_operational_report.v1"
DECISIONS = {"BLOCKED", "DRY_RUN", "SUBMITTED", "MONITOR", "COMPLETE", "NO_TRADE"}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _stable_json_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_checksum(record: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(record))
    payload.pop("record_sha256", None)
    return _stable_json_sha256(payload)


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return payload


def _path_record(path: Path, artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": _file_sha256(path),
        "bytes": path.stat().st_size,
        "schema_version": artifact.get("schema_version"),
        "artifact_type": artifact.get("artifact_type"),
        "run_id": artifact.get("run_id"),
        "generated_at_utc": artifact.get("generated_at_utc"),
        "artifact_sha256": artifact.get("artifact_sha256"),
    }


def _validate_audit(path: Path) -> dict[str, Any]:
    artifact = _load_json_object(path, "Step 8 audit artifact")
    if artifact.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise RuntimeError("Step 8 audit schema_version is not supported")
    if artifact.get("artifact_type") != "paper_run_audit_record":
        raise RuntimeError("Step 8 audit artifact_type must be paper_run_audit_record")
    if artifact.get("paper_only") is not True:
        raise RuntimeError("Step 8 audit paper_only must be true")
    if artifact.get("artifact_sha256") != _audit_checksum(artifact):
        raise RuntimeError("Step 8 audit artifact_sha256 mismatch")
    return artifact


def _validate_reconciliation(path: Path) -> dict[str, Any]:
    artifact = _load_json_object(path, "Step 7 reconciliation artifact")
    if artifact.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        raise RuntimeError("Step 7 reconciliation schema_version is not supported")
    if artifact.get("artifact_type") != "paper_submit_reconciliation":
        raise RuntimeError("Step 7 reconciliation artifact_type must be paper_submit_reconciliation")
    if artifact.get("paper_only") is not True:
        raise RuntimeError("Step 7 reconciliation paper_only must be true")
    if artifact.get("live_port_supported") is not False:
        raise RuntimeError("Step 7 reconciliation live_port_supported must be false")
    if artifact.get("artifact_sha256") != _reconciliation_checksum(artifact):
        raise RuntimeError("Step 7 reconciliation artifact_sha256 mismatch")
    safety = artifact.get("safety")
    if not isinstance(safety, dict):
        raise RuntimeError("Step 7 reconciliation safety section must be an object")
    expected_safety = {
        "operator_confirmed_yes": True,
        "paper_env_required": True,
        "ibkr_port": 7497,
        "orders_cancelled": False,
        "circuit_breaker_reset": False,
        "live_orders_allowed": False,
    }
    for key, value in expected_safety.items():
        if safety.get(key) != value:
            raise RuntimeError(f"Step 7 reconciliation safety.{key} must be {value!r}")
    return artifact


def _validate_order_reconciliation(path: Path) -> dict[str, Any]:
    artifact = _load_json_object(path, "Durable order reconciliation artifact")
    if artifact.get("schema_version") != ORDER_RECONCILE_SCHEMA_VERSION:
        raise RuntimeError("Durable order reconciliation schema_version is not supported")
    if artifact.get("artifact_type") != "paper_order_reconciliation":
        raise RuntimeError("Durable order reconciliation artifact_type must be paper_order_reconciliation")
    if artifact.get("paper_only") is not True:
        raise RuntimeError("Durable order reconciliation paper_only must be true")
    if artifact.get("artifact_sha256") != _order_reconcile_checksum(artifact):
        raise RuntimeError("Durable order reconciliation artifact_sha256 mismatch")
    safety = artifact.get("safety")
    if not isinstance(safety, dict):
        raise RuntimeError("Durable order reconciliation safety section must be an object")
    expected_safety = {
        "paper_env_required": True,
        "ibkr_port": 7497,
        "broker_connected_for_reconciliation": True,
        "orders_submitted": False,
        "orders_cancelled": False,
        "circuit_breaker_reset": False,
        "human_yes_consumed": False,
        "prior_artifacts_mutated": False,
        "live_orders_allowed": False,
    }
    for key, value in expected_safety.items():
        if safety.get(key) != value:
            raise RuntimeError(f"Durable order reconciliation safety.{key} must be {value!r}")
    return artifact


def _broker_order_ids(rows: Any, *, label: str) -> set[str]:
    if not isinstance(rows, list):
        raise RuntimeError(f"{label} must be a list")
    broker_ids = {
        str(row.get("broker_order_id"))
        for row in rows
        if isinstance(row, dict) and row.get("broker_order_id") not in {None, ""}
    }
    if len(broker_ids) != len(rows):
        raise RuntimeError(f"{label} must contain unique non-empty broker_order_id values")
    return broker_ids


def _validate_cross_links(
    *,
    audit_path: Path,
    audit: Mapping[str, Any],
    reconciliation_path: Path | None,
    reconciliation: Mapping[str, Any] | None,
    order_reconciliation_path: Path | None,
    order_reconciliation: Mapping[str, Any] | None,
) -> None:
    audit_paths = audit.get("artifact_paths")
    if not isinstance(audit_paths, dict):
        raise RuntimeError("Step 8 audit artifact_paths must be an object")
    if reconciliation_path is not None and reconciliation is not None:
        audit_reconciliation = audit_paths.get("reconciliation")
        if not isinstance(audit_reconciliation, dict):
            raise RuntimeError("Step 8 audit does not reference the supplied Step 7 reconciliation")
        if audit_reconciliation.get("sha256") != _file_sha256(reconciliation_path):
            raise RuntimeError("Step 8 audit reconciliation sha256 does not match supplied Step 7 artifact")
    if order_reconciliation_path is not None and order_reconciliation is not None:
        if reconciliation_path is None or reconciliation is None:
            raise RuntimeError("Durable order reconciliation requires the matching Step 7 reconciliation artifact")
        if order_reconciliation.get("source_reconciliation_sha256") != _file_sha256(reconciliation_path):
            raise RuntimeError("Durable order reconciliation source sha256 does not match Step 7 artifact")
        if order_reconciliation.get("source_reconciliation_artifact_sha256") != reconciliation.get(
            "artifact_sha256"
        ):
            raise RuntimeError("Durable order reconciliation source artifact checksum does not match Step 7")
        broker_responses = reconciliation.get("broker_responses")
        durable_results = order_reconciliation.get("results")
        step7_ids = _broker_order_ids(broker_responses, label="Step 7 broker_responses")
        durable_ids = _broker_order_ids(durable_results, label="Durable order reconciliation results")
        if durable_ids != step7_ids:
            raise RuntimeError("Durable order reconciliation broker_order_id set does not match Step 7")
        order_count = order_reconciliation.get("order_count")
        if order_count != reconciliation.get("order_count") or order_count != len(step7_ids):
            raise RuntimeError("Durable order reconciliation order_count does not cover Step 7 orders")
        if order_reconciliation.get("status_found_count") != order_count:
            raise RuntimeError("Durable order reconciliation status_found_count must equal order_count")
        if order_reconciliation.get("query_error_count") != 0:
            raise RuntimeError("Durable order reconciliation query_error_count must be 0")
    if str(audit_path) == str(reconciliation_path) or str(audit_path) == str(order_reconciliation_path):
        raise RuntimeError("Ledger source artifacts must be distinct files")


def _validate_decision(
    *,
    decision: str,
    audit: Mapping[str, Any],
    reconciliation: Mapping[str, Any] | None,
    order_reconciliation: Mapping[str, Any] | None,
) -> None:
    audit_status = audit.get("operator_visible_status")
    if decision == "NO_TRADE":
        if audit_status not in {"BLOCKED", "DRY_RUN"}:
            raise RuntimeError("NO_TRADE ledger decision requires a BLOCKED or DRY_RUN Step 8 audit")
        if reconciliation is not None or order_reconciliation is not None:
            raise RuntimeError("NO_TRADE ledger decision must not include submission or order reconciliation artifacts")
        return
    if decision in {"SUBMITTED", "MONITOR", "COMPLETE"} and reconciliation is None:
        raise RuntimeError(f"{decision} ledger decision requires a Step 7 reconciliation artifact")
    if decision == "SUBMITTED" and audit_status != "SUBMITTED":
        raise RuntimeError("SUBMITTED ledger decision requires Step 8 status SUBMITTED")
    if decision == "MONITOR":
        if audit_status != "SUBMITTED":
            raise RuntimeError("MONITOR ledger decision requires Step 8 status SUBMITTED")
        if order_reconciliation is not None and order_reconciliation.get("status") == "RECONCILED":
            raise RuntimeError("MONITOR is inconsistent with a clean durable reconciliation artifact")
    if decision == "COMPLETE":
        if audit_status not in {"SUBMITTED", "COMPLETE"}:
            raise RuntimeError("COMPLETE ledger decision requires Step 8 status SUBMITTED or COMPLETE")
        if order_reconciliation is None:
            raise RuntimeError("COMPLETE ledger decision requires a durable order reconciliation artifact")
        if order_reconciliation.get("status") != "RECONCILED":
            raise RuntimeError("COMPLETE ledger decision requires durable order reconciliation status RECONCILED")
    if decision in {"BLOCKED", "DRY_RUN"} and audit_status != decision:
        raise RuntimeError(f"{decision} ledger decision requires Step 8 status {decision}")


def _summaries(
    *,
    audit: Mapping[str, Any],
    reconciliation: Mapping[str, Any] | None,
    order_reconciliation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    broker_responses = [] if reconciliation is None else reconciliation.get("broker_responses", [])
    if not isinstance(broker_responses, list):
        broker_responses = []
    blockers = audit.get("unresolved_blockers", [])
    if not isinstance(blockers, list):
        blockers = []
    return {
        "audit_status": audit.get("operator_visible_status"),
        "step7_status": None if reconciliation is None else reconciliation.get("status"),
        "durable_reconciliation_status": None
        if order_reconciliation is None
        else order_reconciliation.get("status"),
        "candidate_count": audit.get("inputs", {}).get("candidate_count")
        if isinstance(audit.get("inputs"), dict)
        else None,
        "submitted_order_count": len(broker_responses),
        "durable_status_found_count": None
        if order_reconciliation is None
        else order_reconciliation.get("status_found_count"),
        "durable_query_error_count": None
        if order_reconciliation is None
        else order_reconciliation.get("query_error_count"),
        "unresolved_blocker_count": len(blockers),
    }


def _order_records(
    *,
    reconciliation: Mapping[str, Any] | None,
    order_reconciliation: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    broker_responses = [] if reconciliation is None else reconciliation.get("broker_responses", [])
    if not isinstance(broker_responses, list):
        broker_responses = []
    durable_results = [] if order_reconciliation is None else order_reconciliation.get("results", [])
    if not isinstance(durable_results, list):
        durable_results = []
    durable_by_id = {
        str(row.get("broker_order_id")): row
        for row in durable_results
        if isinstance(row, dict) and row.get("broker_order_id") not in {None, ""}
    }

    orders: list[dict[str, Any]] = []
    for row in broker_responses:
        if not isinstance(row, dict):
            continue
        broker_order_id = row.get("broker_order_id")
        durable = durable_by_id.get(str(broker_order_id)) if broker_order_id not in {None, ""} else None
        broker_status = durable.get("broker_status") if isinstance(durable, dict) else None
        if not isinstance(broker_status, dict):
            broker_status = {}
        orders.append(
            {
                "sequence": row.get("sequence"),
                "ticker": row.get("ticker"),
                "direction": row.get("direction"),
                "submitted_quantity": row.get("submitted_quantity"),
                "limit_price": row.get("limit_price"),
                "broker_order_id": broker_order_id,
                "submitted_at_utc": row.get("submitted_at_utc"),
                "initial_fill_poll": row.get("initial_fill_poll"),
                "durable_status_found": None if durable is None else durable.get("status_found"),
                "durable_query_ok": None if durable is None else durable.get("query_ok"),
                "durable_error": None if durable is None else durable.get("error"),
                "broker_status": None if durable is None else broker_status,
            }
        )
    return orders


def _fill_records(order_records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    for order in order_records:
        broker_status = order.get("broker_status")
        if not isinstance(broker_status, dict):
            continue
        filled_quantity = broker_status.get("filled_quantity")
        if filled_quantity in {None, 0, 0.0}:
            continue
        fills.append(
            {
                "ticker": order.get("ticker"),
                "direction": order.get("direction"),
                "broker_order_id": order.get("broker_order_id"),
                "filled_quantity": filled_quantity,
                "avg_price": broker_status.get("avg_price"),
                "last_fill_price": broker_status.get("last_fill_price"),
                "broker_status": broker_status.get("status"),
            }
        )
    return fills


def _build_report(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_type": "paper_operational_report",
        "run_id": record["run_id"],
        "generated_at_utc": record["generated_at_utc"],
        "paper_only": True,
        "trading_date": record["trading_date"],
        "decision": record["decision"],
        "decision_reason": record["decision_reason"],
        "summary": record["summary"],
        "orders": record["orders"],
        "fills": record["fills"],
        "artifact_paths": record["artifact_paths"],
        "circuit_breaker_events": record["circuit_breaker_events"],
        "ledger_record_sha256": record["record_sha256"],
    }


def _write_json(path: Path, payload: Mapping[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise RuntimeError(f"Output artifact already exists: {path}. Pass --overwrite-report to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
            encoding="utf-8",
        )
        if overwrite:
            temp_path.replace(path)
        else:
            try:
                os.link(temp_path, path)
            except FileExistsError as exc:
                raise RuntimeError(
                    f"Output artifact already exists: {path}. Pass --overwrite-report to replace it."
                ) from exc
    finally:
        if temp_path.exists():
            temp_path.unlink()


def preflight_report_path(path: Path, *, overwrite: bool) -> None:
    """Fail fast on report path problems before appending the ledger."""
    if path.exists() and not overwrite:
        raise RuntimeError(f"Output artifact already exists: {path}. Pass --overwrite-report to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text("", encoding="utf-8")
    finally:
        if temp_path.exists():
            temp_path.unlink()


def append_record(ledger_path: Path, record: Mapping[str, Any]) -> None:
    """Append one checksum-bearing record to the local JSONL ledger."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=_json_default) + "\n")


def build_operational_record(
    *,
    trading_date: date,
    decision: str,
    decision_reason: str,
    audit_path: Path,
    reconciliation_path: Path | None = None,
    order_reconciliation_path: Path | None = None,
    circuit_breaker_events: list[str] | None = None,
    notes: list[str] | None = None,
    generated_at: datetime,
    run_id: str,
) -> dict[str, Any]:
    """Validate paper artifacts and build one append-only daily ledger record."""
    if decision not in DECISIONS:
        raise RuntimeError(f"Decision must be one of {sorted(DECISIONS)}")
    normalized_reason = decision_reason.strip()
    if not normalized_reason:
        raise RuntimeError("--decision-reason must be non-empty")
    audit = _validate_audit(audit_path)
    reconciliation = None if reconciliation_path is None else _validate_reconciliation(reconciliation_path)
    order_reconciliation = (
        None
        if order_reconciliation_path is None
        else _validate_order_reconciliation(order_reconciliation_path)
    )
    _validate_cross_links(
        audit_path=audit_path,
        audit=audit,
        reconciliation_path=reconciliation_path,
        reconciliation=reconciliation,
        order_reconciliation_path=order_reconciliation_path,
        order_reconciliation=order_reconciliation,
    )
    _validate_decision(
        decision=decision,
        audit=audit,
        reconciliation=reconciliation,
        order_reconciliation=order_reconciliation,
    )
    artifact_paths: dict[str, Any] = {"audit": _path_record(audit_path, audit)}
    if reconciliation_path is not None and reconciliation is not None:
        artifact_paths["reconciliation"] = _path_record(reconciliation_path, reconciliation)
    if order_reconciliation_path is not None and order_reconciliation is not None:
        artifact_paths["order_reconciliation"] = _path_record(
            order_reconciliation_path,
            order_reconciliation,
        )
    order_records = _order_records(
        reconciliation=reconciliation,
        order_reconciliation=order_reconciliation,
    )
    record = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "artifact_type": LEDGER_RECORD_TYPE,
        "run_id": run_id,
        "generated_at_utc": generated_at.astimezone(UTC).isoformat(),
        "paper_only": True,
        "trading_date": trading_date.isoformat(),
        "decision": decision,
        "decision_reason": normalized_reason,
        "summary": _summaries(
            audit=audit,
            reconciliation=reconciliation,
            order_reconciliation=order_reconciliation,
        ),
        "orders": order_records,
        "fills": _fill_records(order_records),
        "artifact_paths": artifact_paths,
        "circuit_breaker_events": [event.strip() for event in circuit_breaker_events or [] if event.strip()],
        "notes": [note.strip() for note in notes or [] if note.strip()],
        "safety_assertions": {
            "paper_only": True,
            "ledger_append_only": True,
            "broker_connected": False,
            "orders_submitted": False,
            "orders_cancelled": False,
            "circuit_breaker_reset": False,
            "prior_artifacts_mutated": False,
            "live_orders_allowed": False,
        },
    }
    record["record_sha256"] = _record_checksum(record)
    return record


def write_report(path: Path, record: Mapping[str, Any], *, overwrite: bool) -> None:
    """Write the compact JSON report derived from a ledger record."""
    _write_json(path, _build_report(record), overwrite=overwrite)

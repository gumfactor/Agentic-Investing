"""Write the final local paper-run audit record from existing artifacts.

Usage:
    python -m scripts.paper_run_audit_check --blotter .\\local\\paper_stage_blotter.json --status BLOCKED --output .\\local\\paper_run_audit.json

This Step 8 command is a local run-record writer. It validates the Step 6
stage-only blotter and, when supplied, the Step 7 reconciliation artifact, then
writes a separate audit artifact for phase-gate review. It never connects to
IBKR, submits/cancels/reconciles broker orders, mutates prior artifacts,
resets/trips circuit breakers, or consumes human YES.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.paper_inputs_check import CheckRecorder
from scripts.paper_stage_blotter_check import _file_sha256
from scripts.paper_submit_reconcile_check import (
    RECONCILIATION_SCHEMA_VERSION,
    _reconciliation_checksum,
    validate_blotter,
)

AUDIT_SCHEMA_VERSION = "paper_run_audit.v1"
RUN_STATUSES = {"BLOCKED", "DRY_RUN", "SUBMITTED", "FAILED", "COMPLETE"}
GATE_STATUSES = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "UNKNOWN", "MANUAL"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blotter",
        type=Path,
        required=True,
        help="Step 6 paper_stage_blotter.json artifact to validate and include.",
    )
    parser.add_argument(
        "--reconciliation",
        type=Path,
        default=None,
        help="Optional Step 7 paper_submit_reconciliation.json artifact to validate and include.",
    )
    parser.add_argument(
        "--status",
        required=True,
        choices=sorted(RUN_STATUSES),
        help="Operator-visible final run status for this audit record.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Explicit local JSON path for the Step 8 audit artifact.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing audit artifact. Default is fail-closed.",
    )
    parser.add_argument(
        "--readiness-record",
        type=Path,
        default=None,
        help="Optional manual/text Step 1 readiness note or captured command output.",
    )
    for step in range(1, 6):
        parser.add_argument(
            f"--step{step}-status",
            choices=sorted(GATE_STATUSES),
            default="UNKNOWN",
            help=f"Recorded status for Step {step} preflight gate.",
        )
    parser.add_argument(
        "--blocker",
        action="append",
        default=[],
        help="Known unresolved blocker to include. Repeat for multiple blockers.",
    )
    parser.add_argument(
        "--next-action",
        default=None,
        help="Operator-visible next action. Defaults from --status when omitted.",
    )
    return parser.parse_args(argv)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _stable_json_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _audit_checksum(artifact: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(artifact))
    payload.pop("artifact_sha256", None)
    return _stable_json_sha256(payload)


def _write_artifact(path: Path, artifact: Mapping[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise RuntimeError(f"Output artifact already exists: {path}. Pass --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True, default=_json_default) + "\n",
            encoding="utf-8",
        )
        if overwrite:
            temp_path.replace(path)
        else:
            try:
                os.link(temp_path, path)
            except FileExistsError as exc:
                raise RuntimeError(
                    f"Output artifact already exists: {path}. Pass --overwrite to replace it."
                ) from exc
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return payload


def _validate_reconciliation(path: Path, blotter_path: Path, blotter: Mapping[str, Any]) -> dict[str, Any]:
    artifact = _load_json_object(path, "Reconciliation artifact")
    if artifact.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        raise RuntimeError("Reconciliation schema_version is not supported")
    if artifact.get("artifact_type") != "paper_submit_reconciliation":
        raise RuntimeError("Reconciliation artifact_type must be paper_submit_reconciliation")
    if artifact.get("paper_only") is not True:
        raise RuntimeError("Reconciliation paper_only must be true")
    if artifact.get("live_port_supported") is not False:
        raise RuntimeError("Reconciliation live_port_supported must be false")
    if artifact.get("artifact_sha256") != _reconciliation_checksum(artifact):
        raise RuntimeError("Reconciliation artifact_sha256 mismatch")
    if artifact.get("source_blotter_sha256") != _file_sha256(blotter_path):
        raise RuntimeError("Reconciliation source_blotter_sha256 does not match the Step 6 file")
    if artifact.get("source_blotter_artifact_sha256") != blotter.get("artifact_sha256"):
        raise RuntimeError("Reconciliation source_blotter_artifact_sha256 does not match Step 6")
    if artifact.get("source_candidate_rows_sha256") != blotter.get("candidate_rows_sha256"):
        raise RuntimeError("Reconciliation source_candidate_rows_sha256 does not match Step 6")
    safety = artifact.get("safety")
    if not isinstance(safety, dict):
        raise RuntimeError("Reconciliation safety section must be an object")
    expected = {
        "operator_confirmed_yes": True,
        "paper_env_required": True,
        "ibkr_port": 7497,
        "orders_cancelled": False,
        "circuit_breaker_reset": False,
        "live_orders_allowed": False,
    }
    for key, value in expected.items():
        if safety.get(key) != value:
            raise RuntimeError(f"Reconciliation safety.{key} must be {value!r}")
    broker_responses = artifact.get("broker_responses")
    if not isinstance(broker_responses, list):
        raise RuntimeError("Reconciliation broker_responses must be a list")
    if artifact.get("order_count") != len(broker_responses):
        raise RuntimeError("Reconciliation order_count must match broker_responses length")
    return artifact


def _git_value(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _git_metadata() -> dict[str, Any]:
    return {
        "branch": _git_value(["branch", "--show-current"]),
        "commit": _git_value(["rev-parse", "HEAD"]),
        "dirty": _git_value(["status", "--porcelain"]) not in {None, ""},
    }


def _path_record(path: Path, artifact: Mapping[str, Any] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path),
        "sha256": _file_sha256(path),
        "bytes": path.stat().st_size,
    }
    if artifact is not None:
        record["schema_version"] = artifact.get("schema_version")
        record["run_id"] = artifact.get("run_id")
        record["generated_at_utc"] = artifact.get("generated_at_utc")
        record["artifact_sha256"] = artifact.get("artifact_sha256")
    return record


def _gate_statuses(args: argparse.Namespace) -> dict[str, str]:
    return {f"step{step}": getattr(args, f"step{step}_status") for step in range(1, 6)}


def _default_next_action(status: str, blockers: list[str], reconciliation: Mapping[str, Any] | None) -> str:
    if blockers:
        return "Resolve recorded blockers, then rerun the affected paper preflight gates."
    if status == "BLOCKED":
        return "Resolve the blocking condition before submitting paper orders."
    if status == "DRY_RUN":
        return "Review the Step 6 blotter and run Step 7 with literal YES only when ready."
    if status == "FAILED":
        return "Review the reconciliation failure artifact and decide whether manual broker follow-up is required."
    if status == "SUBMITTED" and reconciliation is not None:
        return "Monitor paper broker state and retain this audit record for phase-gate review."
    return "Retain this audit record for the paper-trading phase gate."


def _validate_status_consistency(
    status: str,
    blockers: list[str],
    reconciliation: Mapping[str, Any] | None,
) -> None:
    if status in {"SUBMITTED", "COMPLETE"}:
        if reconciliation is None:
            raise RuntimeError(f"Status {status} requires a Step 7 reconciliation artifact")
        if reconciliation.get("status") != "SUBMITTED":
            raise RuntimeError(f"Status {status} requires reconciliation status SUBMITTED")
    if status == "FAILED":
        if reconciliation is None:
            raise RuntimeError("Status FAILED requires a Step 7 failure reconciliation artifact")
        if reconciliation.get("status") != "FAILED":
            raise RuntimeError("Status FAILED requires reconciliation status FAILED")
    if status == "DRY_RUN" and reconciliation is not None:
        raise RuntimeError("Status DRY_RUN must not include a reconciliation artifact")
    if status == "BLOCKED" and reconciliation is not None and reconciliation.get("status") == "SUBMITTED":
        raise RuntimeError("Status BLOCKED is inconsistent with a submitted reconciliation artifact")
    if status == "COMPLETE" and blockers:
        raise RuntimeError("Status COMPLETE cannot include unresolved blockers")


def _build_artifact(
    *,
    args: argparse.Namespace,
    blotter: Mapping[str, Any],
    reconciliation: Mapping[str, Any] | None,
    now: datetime,
    run_id: str,
    git_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    blockers = [blocker for blocker in args.blocker if blocker.strip()]
    _validate_status_consistency(args.status, blockers, reconciliation)
    artifact_paths: dict[str, Any] = {
        "blotter": _path_record(args.blotter, blotter),
    }
    if args.reconciliation is not None and reconciliation is not None:
        artifact_paths["reconciliation"] = _path_record(args.reconciliation, reconciliation)
    if args.readiness_record is not None:
        if not args.readiness_record.exists():
            raise RuntimeError(f"Readiness record does not exist: {args.readiness_record}")
        artifact_paths["readiness_record"] = _path_record(args.readiness_record)

    validation = {
        "blotter_schema_valid": True,
        "blotter_checksums_valid": True,
        "reconciliation_schema_valid": reconciliation is not None,
        "reconciliation_checksums_valid": reconciliation is not None,
        "status_consistency_valid": True,
    }
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_type": "paper_run_audit_record",
        "run_id": run_id,
        "generated_at_utc": now.astimezone(UTC).isoformat(),
        "paper_only": True,
        "operator_visible_status": args.status,
        "inputs": {
            "gate_statuses": _gate_statuses(args),
            "step6_blotter_run_id": blotter.get("run_id"),
            "step7_reconciliation_run_id": None if reconciliation is None else reconciliation.get("run_id"),
            "candidate_count": len(blotter.get("candidate_rows", [])),
            "step7_status": None if reconciliation is None else reconciliation.get("status"),
        },
        "artifact_paths": artifact_paths,
        "artifact_hashes": {
            key: {"sha256": value["sha256"], "artifact_sha256": value.get("artifact_sha256")}
            for key, value in artifact_paths.items()
        },
        "validation_summary": validation,
        "unresolved_blockers": blockers,
        "next_action": args.next_action
        or _default_next_action(args.status, blockers, reconciliation),
        "git": dict(git_metadata),
        "command_versions": {
            "python": platform.python_version(),
            "module": "scripts.paper_run_audit_check",
            "audit_schema_version": AUDIT_SCHEMA_VERSION,
            "blotter_schema_version": blotter.get("schema_version"),
            "reconciliation_schema_version": None if reconciliation is None else reconciliation.get("schema_version"),
        },
        "safety_assertions": {
            "broker_connected": False,
            "broker_orders_submitted": False,
            "broker_orders_cancelled": False,
            "broker_reconciliation_performed": False,
            "prior_artifacts_mutated": False,
            "circuit_breaker_reset_or_tripped": False,
            "human_yes_consumed": False,
            "live_orders_allowed": False,
        },
    }


def run(
    argv: list[str] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    git_metadata_factory: Callable[[], Mapping[str, Any]] = _git_metadata,
) -> int:
    args = parse_args(argv)
    load_dotenv()
    recorder = CheckRecorder()
    recorder.info("Paper run audit record check")

    try:
        if args.output.exists() and not args.overwrite:
            raise RuntimeError(f"Output artifact already exists: {args.output}. Pass --overwrite to replace it.")
        if args.output.resolve() == args.blotter.resolve():
            raise RuntimeError("Audit output must be separate from the Step 6 blotter artifact")
        if args.reconciliation is not None and args.output.resolve() == args.reconciliation.resolve():
            raise RuntimeError("Audit output must be separate from the Step 7 reconciliation artifact")

        blotter = validate_blotter(args.blotter)
        reconciliation = (
            None
            if args.reconciliation is None
            else _validate_reconciliation(args.reconciliation, args.blotter, blotter)
        )
        artifact = _build_artifact(
            args=args,
            blotter=blotter,
            reconciliation=reconciliation,
            now=now_fn(),
            run_id=run_id_factory(),
            git_metadata=git_metadata_factory(),
        )
        artifact["artifact_sha256"] = _audit_checksum(artifact)
        _write_artifact(args.output, artifact, overwrite=args.overwrite)
        recorder.info(f"Audit artifact: {args.output}")
        recorder.info(f"Run id: {artifact['run_id']}")
        recorder.info(f"Operator-visible status: {artifact['operator_visible_status']}")
    except Exception as exc:
        recorder.fail(str(exc))

    print()
    if recorder.is_ok:
        print("Paper run audit record: OK")
        return 0

    print("Paper run audit record: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

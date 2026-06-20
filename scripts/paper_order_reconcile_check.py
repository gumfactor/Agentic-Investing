"""Durably reconcile previously submitted IBKR paper orders.

Usage:
    python -m scripts.paper_order_reconcile_check --reconciliation .\\local\\paper_submit_reconciliation.json --output .\\local\\paper_order_reconciliation.json

This command is read-only. It validates an existing Step 7
paper_submit_reconciliation artifact, reconnects to the IBKR paper socket, reads
current broker order/fill state for the recorded broker_order_id values, and
writes a separate local artifact for phase-gate evidence. It never submits
orders, cancels orders, resets/trips circuit breakers, mutates prior artifacts,
or consumes human YES.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.paper_inputs_check import CheckRecorder
from scripts.paper_stage_blotter_check import _file_sha256
from scripts.paper_submit_reconcile_check import (
    RECONCILIATION_SCHEMA_VERSION,
    _reconciliation_checksum,
    _validate_broker_paper_metadata,
)

ORDER_RECONCILE_SCHEMA_VERSION = "paper_order_reconcile.v1"


class OrderStatusBroker(Protocol):
    @property
    def is_paper(self) -> bool:
        ...

    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...

    def get_order_status(self, broker_order_id: str) -> dict[str, Any] | None:
        ...


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reconciliation",
        type=Path,
        required=True,
        help="Step 7 paper_submit_reconciliation.json artifact to reconcile.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Separate local JSON path for the durable order reconciliation artifact.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing artifact.")
    parser.add_argument("--client-id", type=int, default=None, help="Override IBKR client ID for this check.")
    return parser.parse_args(argv)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _stable_json_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _artifact_checksum(artifact: Mapping[str, Any]) -> str:
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


def _paper_env_is_valid(env: Mapping[str, str], recorder: CheckRecorder) -> bool:
    ok = True
    if env.get("PAPER_TRADING", "").strip().lower() != "true":
        recorder.fail("PAPER_TRADING must be explicitly set to true for durable paper order reconciliation")
        ok = False
    if env.get("IBKR_PORT", "").strip() != "7497":
        recorder.fail("IBKR_PORT must be exactly 7497 for durable paper order reconciliation")
        ok = False
    if env.get("PAPER_RUN_CLEARED", "false").strip().lower() == "true":
        recorder.fail("PAPER_RUN_CLEARED=true is a live-trading clearance flag; unset it for paper reconciliation")
        ok = False
    return ok


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return payload


def _validate_source_reconciliation(path: Path) -> dict[str, Any]:
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

    responses = artifact.get("broker_responses")
    if not isinstance(responses, list):
        raise RuntimeError("Step 7 reconciliation broker_responses must be a list")
    if artifact.get("order_count") != len(responses):
        raise RuntimeError("Step 7 reconciliation order_count must match broker_responses length")
    broker_ids = [_broker_order_id(row) for row in responses if isinstance(row, dict)]
    if not any(broker_ids):
        raise RuntimeError("Step 7 reconciliation has no broker_order_id values to reconcile")
    return artifact


def _broker_order_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("broker_order_id")
    if value in {None, ""}:
        return None
    return str(value)


def _default_broker_factory(client_id: int | None) -> OrderStatusBroker:
    from execution.brokers.ibkr import IBKRBroker

    kwargs = {} if client_id is None else {"client_id": client_id}
    return IBKRBroker(**kwargs)


def _status_summary(status: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if status is None:
        return None
    return {
        "broker_order_id": status.get("broker_order_id"),
        "status": status.get("status"),
        "filled_quantity": status.get("filled_quantity"),
        "remaining_quantity": status.get("remaining_quantity"),
        "avg_price": status.get("avg_price"),
        "last_fill_price": status.get("last_fill_price"),
        "why_held": status.get("why_held"),
    }


def _reconcile_orders(
    reconciliation: Mapping[str, Any],
    broker: OrderStatusBroker,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in reconciliation["broker_responses"]:
        if not isinstance(row, dict):
            continue
        broker_order_id = _broker_order_id(row)
        if broker_order_id is None:
            continue
        result = {
            "sequence": row.get("sequence"),
            "ticker": row.get("ticker"),
            "direction": row.get("direction"),
            "submitted_quantity": row.get("submitted_quantity"),
            "limit_price": row.get("limit_price"),
            "broker_order_id": broker_order_id,
            "query_ok": False,
            "status_found": False,
            "broker_status": None,
            "error": None,
        }
        try:
            status = broker.get_order_status(broker_order_id)
            result["query_ok"] = True
            result["status_found"] = status is not None
            result["broker_status"] = _status_summary(status)
        except Exception as exc:
            result["error"] = str(exc)
        results.append(result)
    return results


def _artifact_status(results: list[Mapping[str, Any]]) -> str:
    if any(row.get("error") for row in results):
        return "PARTIAL"
    if all(row.get("status_found") for row in results):
        return "RECONCILED"
    return "UNKNOWN"


def _build_artifact(
    *,
    reconciliation_path: Path,
    reconciliation: Mapping[str, Any],
    results: list[dict[str, Any]],
    run_id: str,
    generated_at: datetime,
) -> dict[str, Any]:
    found_count = sum(1 for row in results if row["status_found"])
    error_count = sum(1 for row in results if row["error"])
    artifact = {
        "schema_version": ORDER_RECONCILE_SCHEMA_VERSION,
        "artifact_type": "paper_order_reconciliation",
        "run_id": run_id,
        "generated_at_utc": generated_at.astimezone(UTC).isoformat(),
        "paper_only": True,
        "status": _artifact_status(results),
        "source_reconciliation_path": str(reconciliation_path),
        "source_reconciliation_run_id": reconciliation.get("run_id"),
        "source_reconciliation_status": reconciliation.get("status"),
        "source_reconciliation_sha256": _file_sha256(reconciliation_path),
        "source_reconciliation_artifact_sha256": reconciliation.get("artifact_sha256"),
        "source_blotter_path": reconciliation.get("source_blotter_path"),
        "source_blotter_sha256": reconciliation.get("source_blotter_sha256"),
        "order_count": len(results),
        "status_found_count": found_count,
        "query_error_count": error_count,
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
    artifact["artifact_sha256"] = _artifact_checksum(artifact)
    return artifact


def run(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    broker_factory: Callable[[int | None], OrderStatusBroker] = _default_broker_factory,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> int:
    args = parse_args(argv)
    load_dotenv()
    env_map = os.environ if env is None else env
    recorder = CheckRecorder()
    recorder.info("Paper durable order reconciliation check")

    broker: OrderStatusBroker | None = None
    artifact: dict[str, Any] | None = None
    try:
        env_ok = _paper_env_is_valid(env_map, recorder)
        if not env_ok:
            raise RuntimeError("Paper environment gates failed")
        if args.output.resolve() == args.reconciliation.resolve():
            raise RuntimeError("Order reconciliation output must be separate from the Step 7 artifact")
        if args.output.exists() and not args.overwrite:
            raise RuntimeError(f"Output artifact already exists: {args.output}. Pass --overwrite to replace it.")

        reconciliation = _validate_source_reconciliation(args.reconciliation)
        broker = broker_factory(args.client_id)
        _validate_broker_paper_metadata(broker)
        if not broker.is_paper:
            raise RuntimeError("Broker adapter did not report paper mode before connection")
        broker.connect()
        _validate_broker_paper_metadata(broker)
        if not broker.is_paper:
            raise RuntimeError("Broker adapter did not report paper mode after connection")

        results = _reconcile_orders(reconciliation, broker)
        artifact = _build_artifact(
            reconciliation_path=args.reconciliation,
            reconciliation=reconciliation,
            results=results,
            run_id=run_id_factory(),
            generated_at=now_fn(),
        )
        _write_artifact(args.output, artifact, overwrite=args.overwrite)
        recorder.info(f"Order reconciliation artifact: {args.output}")
        recorder.info(f"Statuses found: {artifact['status_found_count']}/{artifact['order_count']}")
        recorder.info(f"Query errors: {artifact['query_error_count']}")
    except Exception as exc:
        recorder.fail(str(exc))
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception as exc:
                recorder.fail(f"Broker disconnect failed: {exc}")

    print()
    if recorder.is_ok and artifact is not None and artifact["status"] == "RECONCILED":
        print(f"Paper durable order reconciliation: {artifact['status']}")
        return 0

    if artifact is not None:
        print(f"Paper durable order reconciliation: {artifact['status']}")
        print("- Not all broker order IDs were durably reconciled; review the artifact before proceeding.")
        return 1

    print("Paper durable order reconciliation: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

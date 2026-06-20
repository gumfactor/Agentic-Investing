"""Run IBKR paper what-if validation for a Step 6 blotter.

Usage:
    python -m scripts.paper_whatif_check --blotter .\\local\\paper_stage_blotter.json --output .\\local\\paper_whatif_validation.json

This Step 7.5 command validates the exact Step 6 paper blotter against IBKR's
paper what-if order path. It connects to paper TWS/Gateway, sends
non-transmitting what-if validation requests, records each broker response, and
writes a local artifact. It does not submit orders, cancel orders, reconcile
fills, mutate prior artifacts, or consume human YES.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import uuid
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.oms.order import Order
from scripts.paper_inputs_check import CheckRecorder
from scripts.paper_stage_blotter_check import _file_sha256
from scripts.paper_submit_reconcile_check import (
    _order_from_row,
    _paper_env_is_valid,
    _validate_broker_paper_metadata,
    validate_blotter,
)

WHATIF_SCHEMA_VERSION = "paper_whatif_validation.v1"


class WhatIfBroker(Protocol):
    @property
    def is_paper(self) -> bool:
        ...

    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...

    def what_if_order(self, order: Order) -> dict[str, Any]:
        ...


def parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--blotter", type=Path, required=True, help="Step 6 paper blotter artifact.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Explicit local JSON path for the what-if validation artifact.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing artifact.")
    parser.add_argument("--client-id", type=int, default=None, help="Override IBKR client ID for this validation.")
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


def _default_broker_factory(client_id: int | None) -> WhatIfBroker:
    from execution.brokers.ibkr import IBKRBroker

    kwargs = {} if client_id is None else {"client_id": client_id}
    return IBKRBroker(**kwargs)


def _response_summary(response: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": response.get("status"),
        "warning_text": response.get("warning_text"),
        "commission": response.get("commission"),
        "commission_currency": response.get("commission_currency"),
        "init_margin_change": response.get("init_margin_change"),
        "maint_margin_change": response.get("maint_margin_change"),
    }


def _run_what_if(
    *,
    blotter: Mapping[str, Any],
    broker: WhatIfBroker,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in blotter["candidate_rows"]:
        order = _order_from_row(row, str(blotter.get("strategy_id", "")))
        result = {
            "sequence": row["sequence"],
            "ticker": row["ticker"],
            "direction": row["direction"],
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "fractional_quantity": abs(order.quantity - round(order.quantity)) > 1e-9,
            "accepted": False,
            "error": None,
            "broker_response": None,
        }
        try:
            response = broker.what_if_order(order)
            result["accepted"] = True
            result["broker_response"] = _response_summary(response)
        except Exception as exc:
            result["error"] = str(exc)
        results.append(result)
    return results


def _build_artifact(
    *,
    blotter_path: Path,
    blotter: Mapping[str, Any],
    results: list[dict[str, Any]],
    run_id: str,
    generated_at: datetime,
) -> dict[str, Any]:
    accepted = sum(1 for row in results if row["accepted"])
    rejected = len(results) - accepted
    fractional = sum(1 for row in results if row["fractional_quantity"])
    artifact = {
        "schema_version": WHATIF_SCHEMA_VERSION,
        "artifact_type": "paper_whatif_validation",
        "run_id": run_id,
        "generated_at_utc": generated_at.astimezone(UTC).isoformat(),
        "paper_only": True,
        "transmit_orders": False,
        "human_yes_consumed": False,
        "source_blotter_path": str(blotter_path),
        "source_blotter_run_id": blotter.get("run_id"),
        "source_blotter_sha256": _file_sha256(blotter_path),
        "source_blotter_artifact_sha256": blotter.get("artifact_sha256"),
        "source_candidate_rows_sha256": blotter.get("candidate_rows_sha256"),
        "order_count": len(results),
        "accepted_count": accepted,
        "rejected_count": rejected,
        "fractional_quantity_count": fractional,
        "status": "PASS" if rejected == 0 else "FAILED",
        "results": results,
        "safety": {
            "paper_env_required": True,
            "ibkr_port": 7497,
            "broker_connected_for_what_if": True,
            "orders_submitted": False,
            "orders_cancelled": False,
            "fills_reconciled": False,
            "live_orders_allowed": False,
        },
    }
    artifact["artifact_sha256"] = _artifact_checksum(artifact)
    return artifact


def run(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    broker_factory: Callable[[int | None], WhatIfBroker] = _default_broker_factory,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> int:
    args = parse_args(argv)
    load_dotenv()
    env_map = os.environ if env is None else env
    recorder = CheckRecorder()
    recorder.info("Paper what-if validation check")

    broker: WhatIfBroker | None = None
    artifact: dict[str, Any] | None = None
    try:
        env_ok = _paper_env_is_valid(env_map, recorder)
        if not env_ok:
            raise RuntimeError("Paper environment gates failed")
        if args.output.resolve() == args.blotter.resolve():
            raise RuntimeError("What-if output must be separate from the Step 6 blotter artifact")
        if args.output.exists() and not args.overwrite:
            raise RuntimeError(f"Output artifact already exists: {args.output}. Pass --overwrite to replace it.")
        blotter = validate_blotter(args.blotter)
        broker = broker_factory(args.client_id)
        _validate_broker_paper_metadata(broker)
        if not broker.is_paper:
            raise RuntimeError("Broker adapter did not report paper mode before connection")
        broker.connect()
        _validate_broker_paper_metadata(broker)
        if not broker.is_paper:
            raise RuntimeError("Broker adapter did not report paper mode after connection")

        results = _run_what_if(blotter=blotter, broker=broker)
        artifact = _build_artifact(
            blotter_path=args.blotter,
            blotter=blotter,
            results=results,
            run_id=run_id_factory(),
            generated_at=now_fn(),
        )
        _write_artifact(args.output, artifact, overwrite=args.overwrite)
        recorder.info(f"What-if artifact: {args.output}")
        recorder.info(f"Accepted: {artifact['accepted_count']}/{artifact['order_count']}")
        recorder.info(f"Fractional quantity rows: {artifact['fractional_quantity_count']}")
    except Exception as exc:
        recorder.fail(str(exc))
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception as exc:
                recorder.fail(f"Broker disconnect failed: {exc}")

    print()
    if recorder.is_ok and artifact is not None and artifact["status"] == "PASS":
        print("Paper what-if validation: OK")
        return 0

    print("Paper what-if validation: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    if artifact is not None:
        for row in artifact["results"]:
            if not row["accepted"]:
                print(f"- {row['sequence']} {row['ticker']} {row['direction']}: {row['error']}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

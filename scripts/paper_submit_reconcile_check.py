"""Validate, display, and optionally submit a Step 6 paper blotter.

Usage:
    python -m scripts.paper_submit_reconcile_check --blotter .\\local\\paper_stage_blotter.json

By default this Step 7 command is dry-run validation/display only. Actual paper
submission requires --confirm YES, explicit paper environment gates, and a
separate reconciliation output artifact. Tests should inject a broker factory;
do not connect to a real broker in automated validation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.oms.order import Order, OrderSide
from scripts.paper_inputs_check import CheckRecorder
from scripts.paper_stage_blotter_check import (
    BLOTTER_SCHEMA_VERSION,
    _file_sha256,
    _rows_checksum,
    _stable_sha256,
)
from scripts.paper_stage_blotter_check import (
    _artifact_checksum as _stage_artifact_checksum,
)

RECONCILIATION_SCHEMA_VERSION = "paper_submit_reconcile.v1"
SUBMITTED_STATUSES = {"SUBMITTED", "PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCELLED"}


class PaperBroker(Protocol):
    @property
    def is_paper(self) -> bool:
        ...

    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...

    def submit_order(self, order: Order) -> str:
        ...

    def get_fill(self, broker_order_id: str) -> dict[str, Any] | None:
        ...


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blotter",
        type=Path,
        required=True,
        help="Step 6 paper_stage_blotter.json artifact to validate and display.",
    )
    parser.add_argument(
        "--confirm",
        default=None,
        help='Literal YES required for paper broker submission. Omit for dry-run validation/display.',
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Separate reconciliation artifact path required when --confirm YES is supplied.",
    )
    parser.add_argument(
        "--reviewed-blotter-sha256",
        default=None,
        help="SHA-256 of the exact Step 6 blotter file displayed during dry-run; required with --confirm YES.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing the reconciliation output artifact. Default is fail-closed.",
    )
    parser.add_argument(
        "--max-blotter-age-days",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Reject blotters whose generated_at_utc is older than N calendar days "
            "(default 1). Prevents submitting a stale-but-valid-checksum blotter "
            "against outdated prices and positions."
        ),
    )
    return parser.parse_args(argv)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _stable_json_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reconciliation_checksum(artifact: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(artifact))
    payload.pop("artifact_sha256", None)
    return _stable_json_sha256(payload)


def _write_artifact(path: Path, artifact: Mapping[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise RuntimeError(f"Output artifact already exists: {path}. Pass --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")
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
        recorder.fail("PAPER_TRADING must be explicitly set to true for Step 7 paper submission preflight")
        ok = False
    if env.get("IBKR_PORT", "").strip() != "7497":
        recorder.fail("IBKR_PORT must be exactly 7497 for Step 7; live port 7496 is never accepted")
        ok = False
    if env.get("PAPER_RUN_CLEARED", "false").strip().lower() == "true":
        recorder.fail("PAPER_RUN_CLEARED=true is a live-trading clearance flag; unset it for Step 7 paper flow")
        ok = False
    return ok


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Blotter artifact is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Blotter artifact must be a JSON object")
    return payload


def _require_exact(payload: Mapping[str, Any], key: str, expected: Any) -> None:
    if payload.get(key) != expected:
        raise RuntimeError(f"Blotter {key} must be {expected!r}; got {payload.get(key)!r}")


def _validate_safety_flags(artifact: Mapping[str, Any]) -> None:
    safety = artifact.get("safety")
    if not isinstance(safety, dict):
        raise RuntimeError("Blotter safety section must be an object")
    expected_false = {
        "broker_connected",
        "broker_order_ids_present",
        "order_manager_registered",
        "orders_submitted",
        "orders_cancelled",
        "fills_reconciled",
        "human_yes_consumed",
    }
    for key in expected_false:
        if safety.get(key) is not False:
            raise RuntimeError(f"Blotter safety.{key} must be false before Step 7")


def _validate_provenance(artifact: Mapping[str, Any]) -> None:
    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict):
        raise RuntimeError("Blotter provenance section must be an object")
    for label, path_key, checksum_key in (
        ("strategy config", "strategy_config_path", "strategy_config_sha256"),
        ("portfolio input", "portfolio_input_path", "portfolio_input_sha256"),
    ):
        raw_path = provenance.get(path_key)
        expected_checksum = provenance.get(checksum_key)
        if not isinstance(raw_path, str) or not raw_path:
            raise RuntimeError(f"Blotter provenance missing {path_key}")
        if not isinstance(expected_checksum, str) or len(expected_checksum) != 64:
            raise RuntimeError(f"Blotter provenance missing valid {checksum_key}")
        source_path = Path(raw_path)
        if not source_path.exists():
            raise RuntimeError(f"Blotter provenance {label} file is not available for checksum validation: {source_path}")
        actual_checksum = _file_sha256(source_path)
        if actual_checksum != expected_checksum:
            raise RuntimeError(f"Blotter provenance {label} checksum mismatch")

    gate_inputs = provenance.get("gate_inputs")
    gate_checksum = provenance.get("gate_inputs_sha256")
    if not isinstance(gate_inputs, dict):
        raise RuntimeError("Blotter provenance.gate_inputs must be an object")
    if gate_checksum != _stable_sha256(gate_inputs):
        raise RuntimeError("Blotter provenance.gate_inputs_sha256 mismatch")


def _validate_candidate_row(row: Mapping[str, Any], sequence: int) -> None:
    if row.get("sequence") != sequence:
        raise RuntimeError(f"Candidate row {sequence} has invalid sequence {row.get('sequence')!r}")
    ticker = row.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise RuntimeError(f"Candidate row {sequence} missing ticker")
    direction = row.get("direction")
    if direction not in {"BUY", "SELL"}:
        raise RuntimeError(f"Candidate row {sequence} direction must be BUY or SELL")
    if row.get("review_status") != "LOCAL_STAGE_ONLY":
        raise RuntimeError(f"Candidate row {sequence} review_status must be LOCAL_STAGE_ONLY")
    for key in row:
        lowered = key.lower()
        if "broker" in lowered or lowered in {"order_id", "submitted_at", "filled_at"}:
            raise RuntimeError(f"Candidate row {sequence} contains forbidden broker/order field {key!r}")
    for key, require_positive in (
        ("estimated_shares", True),
        ("estimated_notional", True),
        ("reference_price", True),
        ("current_weight", False),
        ("target_weight", False),
        ("delta_weight", False),
    ):
        value = row.get(key)
        if not isinstance(value, int | float) or not math.isfinite(float(value)):
            raise RuntimeError(f"Candidate row {sequence} {key} must be finite")
        if require_positive and float(value) <= 0:
            raise RuntimeError(f"Candidate row {sequence} {key} must be positive")
    for value in row.values():
        if isinstance(value, str) and value.upper() in SUBMITTED_STATUSES:
            raise RuntimeError(f"Candidate row {sequence} contains submitted/reconciled status {value!r}")


def validate_blotter(path: Path) -> dict[str, Any]:
    artifact = _load_json(path)
    _require_exact(artifact, "schema_version", BLOTTER_SCHEMA_VERSION)
    _require_exact(artifact, "artifact_type", "paper_stage_only_order_blotter")
    _require_exact(artifact, "paper_only", True)
    _require_exact(artifact, "stage_only", True)
    _validate_safety_flags(artifact)
    source = artifact.get("source")
    if not isinstance(source, dict) or source.get("step5_required") is not True:
        raise RuntimeError("Blotter source.step5_required must be true")
    rows = artifact.get("candidate_rows")
    if not isinstance(rows, list):
        raise RuntimeError("Blotter candidate_rows must be a list")
    for sequence, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Candidate row {sequence} must be an object")
        _validate_candidate_row(row, sequence)
    if artifact.get("candidate_rows_sha256") != _rows_checksum(rows):
        raise RuntimeError("Blotter candidate_rows_sha256 mismatch")
    if artifact.get("artifact_sha256") != _stage_artifact_checksum(artifact):
        raise RuntimeError("Blotter artifact_sha256 mismatch")
    _validate_provenance(artifact)
    return artifact


def _validate_blotter_freshness(artifact: Mapping[str, Any], max_age_days: int) -> None:
    """Reject blotters older than max_age_days calendar days (BUG-051).

    A stale blotter has valid checksums but references outdated prices, positions,
    and target weights. This check prevents accidentally re-submitting yesterday's
    (or older) blotter via the CLI.
    """
    generated_str = artifact.get("generated_at_utc")
    if not isinstance(generated_str, str):
        raise RuntimeError("Blotter generated_at_utc is missing or not a string")
    try:
        generated_dt = datetime.fromisoformat(generated_str.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"Blotter generated_at_utc is not a valid ISO timestamp: {generated_str!r}") from exc
    today_utc = datetime.now(UTC).date()
    generated_date = generated_dt.astimezone(UTC).date()
    age_days = (today_utc - generated_date).days
    if age_days > max_age_days:
        raise RuntimeError(
            f"Blotter is {age_days} calendar day(s) old (generated {generated_date}); "
            f"max allowed is {max_age_days}. Regenerate the blotter with fresh prices, "
            "positions, and target weights before submitting."
        )


def _display_orders(rows: Sequence[Mapping[str, Any]], recorder: CheckRecorder) -> None:
    recorder.info("Full paper order list for operator review")
    if not rows:
        recorder.info("(no candidate orders)")
        return
    header = f"{'#':>3} {'ticker':<10} {'side':<4} {'shares':>12} {'limit_ref':>12} {'notional':>14}"
    recorder.info(header)
    recorder.info("-" * len(header))
    for row in rows:
        recorder.info(
            f"{int(row['sequence']):>3} {str(row['ticker']):<10} {str(row['direction']):<4} "
            f"{float(row['estimated_shares']):>12.6f} {float(row['reference_price']):>12.4f} "
            f"{float(row['estimated_notional']):>14.2f}"
        )


def _display_review_hashes(blotter_path: Path, blotter: Mapping[str, Any], recorder: CheckRecorder) -> None:
    recorder.info(f"Reviewed blotter sha256: {_file_sha256(blotter_path)}")
    recorder.info(f"Candidate rows sha256: {blotter.get('candidate_rows_sha256')}")


def _validate_api_submittable_quantities(rows: Sequence[Mapping[str, Any]]) -> None:
    """Validate that every row's submitted quantity is a whole positive number.

    Uses 'quantity' when present (set by operator overrides or the Airflow submit
    path), falling back to 'estimated_shares' for unmodified blotter rows.
    """
    fractional: list[str] = []
    for row in rows:
        shares = float(row.get("quantity", row["estimated_shares"]))
        if abs(shares - round(shares)) > 1e-9:
            fractional.append(f"{row['sequence']} {row['ticker']} {shares:.6f}")
    if fractional:
        raise RuntimeError(
            "IBKR TWS API rejects fractional-sized stock orders; regenerate a whole-share blotter. "
            f"Fractional rows: {', '.join(fractional)}"
        )


def _validate_api_submittable_prices(rows: Sequence[Mapping[str, Any]]) -> None:
    invalid: list[str] = []
    for row in rows:
        price = float(row["reference_price"])
        cents = round(price * 100)
        if abs(price - cents / 100) > 1e-9:
            invalid.append(f"{row['sequence']} {row['ticker']} {price:.6f}")
    if invalid:
        raise RuntimeError(
            "IBKR TWS API rejected sub-cent stock limit prices; regenerate a cent-rounded blotter. "
            f"Invalid rows: {', '.join(invalid)}"
        )


def _order_from_row(row: Mapping[str, Any], strategy_id: str) -> Order:
    return Order(
        ticker=str(row["ticker"]).strip().upper(),
        side=OrderSide(str(row["direction"])),
        quantity=float(row.get("quantity", row["estimated_shares"])),
        limit_price=float(row["reference_price"]),
        strategy_id=strategy_id,
        notes="Step 7 paper submission from validated Step 6 blotter",
    )


def _default_broker_factory(client_id: int | None = None) -> PaperBroker:
    from execution.brokers.ibkr import IBKRBroker

    kwargs = {} if client_id is None else {"client_id": client_id}
    return IBKRBroker(**kwargs)


def _resolve_client_id(env: Mapping[str, str]) -> int | None:
    raw_client_id = env.get("IBKR_CLIENT_ID")
    if raw_client_id is None or str(raw_client_id).strip() == "":
        return None
    try:
        parsed = int(str(raw_client_id).strip())
    except ValueError as exc:
        raise RuntimeError(f"IBKR_CLIENT_ID must be an integer; got {raw_client_id!r}") from exc
    if parsed <= 0:
        raise RuntimeError(f"IBKR_CLIENT_ID must be positive; got {parsed!r}")
    return parsed


def _validate_broker_paper_metadata(broker: PaperBroker) -> None:
    port = getattr(broker, "port", getattr(broker, "_port", None))
    if port is not None and int(port) != 7497:
        raise RuntimeError(f"Broker adapter port must be 7497 for paper submission; got {port!r}")
    mode = getattr(broker, "connection_mode", None)
    if mode is not None and str(mode).lower() != "paper":
        raise RuntimeError(f"Broker adapter connection_mode must be 'paper'; got {mode!r}")


def _broker_from_factory(
    broker_factory: Callable[..., PaperBroker],
    client_id: int | None,
) -> PaperBroker:
    if client_id is None:
        return broker_factory()
    try:
        return broker_factory(client_id)
    except TypeError:
        return broker_factory(client_id=client_id)


def _submit_orders(
    artifact: Mapping[str, Any],
    broker_factory: Callable[..., PaperBroker],
    *,
    client_id: int | None,
    now_fn: Callable[[], datetime],
    on_progress: Callable[[list[dict[str, Any]], str, int | None, str | None], None] | None = None,
) -> list[dict[str, Any]]:
    broker = _broker_from_factory(broker_factory, client_id)
    connected = False
    try:
        _validate_broker_paper_metadata(broker)
        if not broker.is_paper:
            raise RuntimeError("Broker adapter did not report paper mode before connection")
        broker.connect()
        connected = True
        _validate_broker_paper_metadata(broker)
        if not broker.is_paper:
            raise RuntimeError("Broker adapter did not report paper mode after connection")
        responses: list[dict[str, Any]] = []
        for row in artifact["candidate_rows"]:
            order = _order_from_row(row, str(artifact.get("strategy_id", "")))
            sequence = int(row["sequence"])
            if on_progress is not None:
                on_progress(responses, "SUBMITTING", sequence, None)
            try:
                broker_order_id = broker.submit_order(order)
            except Exception as exc:
                if on_progress is not None:
                    on_progress(responses, "FAILED", sequence, str(exc))
                raise
            response = {
                "sequence": row["sequence"],
                "ticker": row["ticker"],
                "direction": row["direction"],
                "submitted_quantity": order.quantity,
                "limit_price": order.limit_price,
                "broker_order_id": str(broker_order_id),
                "submitted_at_utc": now_fn().astimezone(UTC).isoformat(),
                "initial_fill_poll": None,
            }
            responses.append(response)
            if on_progress is not None:
                on_progress(responses, "PARTIAL", sequence, None)
            try:
                fill = broker.get_fill(broker_order_id)
            except Exception as exc:
                if on_progress is not None:
                    on_progress(responses, "PARTIAL", sequence, str(exc))
                raise
            response["initial_fill_poll"] = fill
            if on_progress is not None:
                on_progress(responses, "PARTIAL", sequence, None)
        return responses
    finally:
        if connected:
            broker.disconnect()


def _build_reconciliation_artifact(
    *,
    blotter_path: Path,
    blotter: Mapping[str, Any],
    broker_responses: list[dict[str, Any]],
    run_id: str,
    now: datetime,
    status: str,
    last_attempted_sequence: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    artifact = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "artifact_type": "paper_submit_reconciliation",
        "run_id": run_id,
        "generated_at_utc": now.astimezone(UTC).isoformat(),
        "paper_only": True,
        "status": status,
        "live_port_supported": False,
        "source_blotter_path": str(blotter_path),
        "source_blotter_run_id": blotter.get("run_id"),
        "source_blotter_sha256": _file_sha256(blotter_path),
        "source_blotter_artifact_sha256": blotter.get("artifact_sha256"),
        "source_candidate_rows_sha256": blotter.get("candidate_rows_sha256"),
        "order_count": len(broker_responses),
        "last_attempted_sequence": last_attempted_sequence,
        "error": error,
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
    artifact["artifact_sha256"] = _reconciliation_checksum(artifact)
    return artifact


def run(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    broker_factory: Callable[..., PaperBroker] = _default_broker_factory,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> int:
    args = parse_args(argv)
    load_dotenv()
    env_map = os.environ if env is None else env
    recorder = CheckRecorder()
    recorder.info("Paper submit/reconcile preflight")

    try:
        env_ok = _paper_env_is_valid(env_map, recorder)
        client_id = _resolve_client_id(env_map)
        blotter = validate_blotter(args.blotter)
        _validate_blotter_freshness(blotter, args.max_blotter_age_days)
        rows = blotter["candidate_rows"]
        _display_orders(rows, recorder)
        _display_review_hashes(args.blotter, blotter, recorder)
        if not env_ok:
            raise RuntimeError("Paper environment gates failed")
        if args.confirm != "YES":
            if args.confirm is not None:
                recorder.fail('Submission confirmation must be the literal string "YES"')
                raise RuntimeError("Literal YES confirmation missing")
            recorder.info("Dry-run only: no broker connection, no submission, no reconciliation artifact")
            print()
            print("Paper submit/reconcile preflight: DRY-RUN OK")
            return 0
        reviewed_checksum = args.reviewed_blotter_sha256
        actual_blotter_checksum = _file_sha256(args.blotter)
        if reviewed_checksum != actual_blotter_checksum:
            raise RuntimeError(
                "--reviewed-blotter-sha256 must match the exact Step 6 blotter file displayed during dry-run"
            )
        _validate_api_submittable_quantities(rows)
        _validate_api_submittable_prices(rows)
        if args.output is None:
            raise RuntimeError("--output is required when --confirm YES is supplied")
        if args.output.resolve() == args.blotter.resolve():
            raise RuntimeError("Reconciliation output must be separate from the Step 6 blotter artifact")
        if args.output.exists() and not args.overwrite:
            raise RuntimeError(f"Output artifact already exists: {args.output}. Pass --overwrite to replace it.")

        run_id = run_id_factory()
        initial_reconciliation = _build_reconciliation_artifact(
            blotter_path=args.blotter,
            blotter=blotter,
            broker_responses=[],
            run_id=run_id,
            now=now_fn(),
            status="STARTED",
        )
        _write_artifact(args.output, initial_reconciliation, overwrite=args.overwrite)

        latest_responses: list[dict[str, Any]] = []
        latest_sequence: int | None = None

        def write_progress(
            responses: list[dict[str, Any]],
            status: str,
            last_attempted_sequence: int | None,
            error: str | None,
        ) -> None:
            nonlocal latest_responses, latest_sequence
            latest_responses = copy.deepcopy(responses)
            latest_sequence = last_attempted_sequence
            progress = _build_reconciliation_artifact(
                blotter_path=args.blotter,
                blotter=blotter,
                broker_responses=copy.deepcopy(responses),
                run_id=run_id,
                now=now_fn(),
                status=status,
                last_attempted_sequence=last_attempted_sequence,
                error=error,
            )
            _write_artifact(args.output, progress, overwrite=True)

        try:
            broker_responses = _submit_orders(
                blotter,
                broker_factory,
                client_id=client_id,
                now_fn=now_fn,
                on_progress=write_progress,
            )
        except Exception as exc:
            # Ensure the output artifact records any accepted broker ids before
            # surfacing the failure to the operator.
            failure = _build_reconciliation_artifact(
                blotter_path=args.blotter,
                blotter=blotter,
                broker_responses=latest_responses,
                run_id=run_id,
                now=now_fn(),
                status="FAILED",
                last_attempted_sequence=latest_sequence,
                error=str(exc),
            )
            _write_artifact(args.output, failure, overwrite=True)
            recorder.fail(str(exc))
            raise RuntimeError(f"Paper submission failed; reconciliation attempt artifact written to {args.output}") from exc
        reconciliation = _build_reconciliation_artifact(
            blotter_path=args.blotter,
            blotter=blotter,
            broker_responses=broker_responses,
            run_id=run_id,
            now=now_fn(),
            status="SUBMITTED",
        )
        _write_artifact(args.output, reconciliation, overwrite=True)
        recorder.info(f"Reconciliation artifact: {args.output}")
        recorder.info(f"Submitted paper orders: {len(broker_responses)}")
    except Exception as exc:
        recorder.fail(str(exc))

    print()
    if recorder.is_ok:
        print("Paper submit/reconcile preflight: SUBMITTED")
        return 0

    print("Paper submit/reconcile preflight: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

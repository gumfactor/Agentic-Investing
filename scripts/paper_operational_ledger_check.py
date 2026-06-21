"""Append a daily paper-trading operational ledger record from local artifacts."""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting.audit.paper_operational_ledger import (
    DECISIONS,
    append_record,
    build_operational_record,
    preflight_report_path,
    write_report,
)
from scripts.paper_inputs_check import CheckRecorder


def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trading-date",
        type=_date_arg,
        required=True,
        help="Paper-trading date represented by this ledger record, as YYYY-MM-DD.",
    )
    parser.add_argument(
        "--decision",
        choices=sorted(DECISIONS),
        required=True,
        help="Operator-visible daily decision to record.",
    )
    parser.add_argument(
        "--decision-reason",
        required=True,
        help="Short operator-visible reason for the daily decision.",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        required=True,
        help="Step 8 paper_run_audit.json artifact to validate and reference.",
    )
    parser.add_argument(
        "--reconciliation",
        type=Path,
        default=None,
        help="Optional Step 7 paper_submit_reconciliation.json artifact.",
    )
    parser.add_argument(
        "--order-reconciliation",
        type=Path,
        default=None,
        help="Optional durable paper_order_reconciliation.json artifact.",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help="Append-only local JSONL ledger path.",
    )
    parser.add_argument(
        "--output-report",
        type=Path,
        default=None,
        help="Optional compact JSON report path derived from the appended ledger record.",
    )
    parser.add_argument(
        "--overwrite-report",
        action="store_true",
        help="Allow replacing --output-report. The ledger itself is always appended.",
    )
    parser.add_argument(
        "--circuit-breaker-event",
        action="append",
        default=[],
        help="Circuit-breaker event or manual fire-drill note to record. Repeat for multiple events.",
    )
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="Additional operator note to include in the ledger record. Repeat for multiple notes.",
    )
    return parser.parse_args(argv)


def run(
    argv: list[str] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> int:
    args = parse_args(argv)
    recorder = CheckRecorder()
    recorder.info("Paper operational ledger record")

    try:
        if args.output_report is not None and args.output_report.resolve() == args.ledger.resolve():
            raise RuntimeError("Output report must be separate from the append-only ledger")
        if args.output_report is not None:
            preflight_report_path(args.output_report, overwrite=args.overwrite_report)
        record = build_operational_record(
            trading_date=args.trading_date,
            decision=args.decision,
            decision_reason=args.decision_reason,
            audit_path=args.audit,
            reconciliation_path=args.reconciliation,
            order_reconciliation_path=args.order_reconciliation,
            circuit_breaker_events=args.circuit_breaker_event,
            notes=args.note,
            generated_at=now_fn(),
            run_id=run_id_factory(),
        )
        append_record(args.ledger, record)
        if args.output_report is not None:
            write_report(args.output_report, record, overwrite=args.overwrite_report)
        recorder.info(f"Ledger appended: {args.ledger}")
        recorder.info(f"Ledger run id: {record['run_id']}")
        recorder.info(f"Decision: {record['decision']}")
        if args.output_report is not None:
            recorder.info(f"Report artifact: {args.output_report}")
    except Exception as exc:
        recorder.fail(str(exc))

    print()
    if recorder.is_ok:
        print("Paper operational ledger record: OK")
        return 0

    print("Paper operational ledger record: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

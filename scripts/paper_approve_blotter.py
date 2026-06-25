"""Interim CLI approval tool for paper-trading blotter artifacts.

Usage:
    python -m scripts.paper_approve_blotter --blotter ./local/blotter_XYZ.json

Reads the blotter artifact from disk, displays all candidates, prompts for
"YES" confirmation, then inserts a row in blotter_approvals so that the
BlotterApprovalSensor in the daily_paper_trading DAG can proceed.

This script bridges the gap until the Streamlit dashboard (M5.8) provides the
same workflow via the F7.4 approval UI. The sensor polls the same
blotter_approvals table regardless of which path created the row.

Safety: the inserted row captures both blotter_sha256 and
confirmed_blotter_sha256 (set equal here to record the operator's explicit
confirmation of the exact file they reviewed). The sensor verifies that these
match the artifact on disk before permitting submission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_blotter(path: Path) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"FAIL: blotter is not valid JSON: {exc}") from exc
    if not isinstance(artifact, dict):
        raise SystemExit("FAIL: blotter must be a JSON object")
    return artifact


def _display_candidates(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("  (no candidate orders)")
        return
    header = f"  {'seq':>3} {'ticker':<10} {'side':<4} {'shares':>12} {'limit':>12} {'notional':>14}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        print(
            f"  {int(row['sequence']):>3} {str(row['ticker']):<10} {str(row['direction']):<4} "
            f"{float(row['estimated_shares']):>12.6f} {float(row['reference_price']):>12.4f} "
            f"{float(row['estimated_notional']):>14.2f}"
        )


def _resolve_selected_ids(
    order_ids_arg: str,
    rows: list[dict[str, Any]],
) -> list[int]:
    """Return list of selected sequence numbers (1-based)."""
    if order_ids_arg.strip().upper() == "ALL":
        return [int(r["sequence"]) for r in rows]
    parts = [p.strip() for p in order_ids_arg.split(",") if p.strip()]
    selected: list[int] = []
    all_seqs = {int(r["sequence"]) for r in rows}
    for p in parts:
        try:
            seq = int(p)
        except ValueError:
            raise SystemExit(f"FAIL: --order-ids contains non-integer value {p!r}") from None
        if seq not in all_seqs:
            raise SystemExit(
                f"FAIL: sequence {seq} not found in blotter candidate rows "
                f"(valid: {sorted(all_seqs)})"
            )
        selected.append(seq)
    return sorted(set(selected))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blotter",
        type=Path,
        required=True,
        help="Local path to the Step 6 paper_stage_blotter.json artifact.",
    )
    parser.add_argument(
        "--order-ids",
        default="ALL",
        help=(
            "Comma-separated list of candidate sequence numbers to approve for submission, "
            "or ALL (default) to approve every candidate."
        ),
    )
    parser.add_argument(
        "--operator",
        default=None,
        help="Operator identifier (email or name). Defaults to USER env var or 'operator'.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Display the order list and exit without writing to the database.",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)

    blotter_path = args.blotter.resolve()
    if not blotter_path.exists():
        print(f"FAIL: blotter file not found: {blotter_path}")
        return 1

    artifact = _load_blotter(blotter_path)

    schema = artifact.get("schema_version", "")
    if schema != "paper_stage_blotter.v1":
        print(f"FAIL: unexpected schema_version {schema!r}; expected paper_stage_blotter.v1")
        return 1
    if artifact.get("paper_only") is not True:
        print("FAIL: blotter paper_only is not true")
        return 1
    if artifact.get("stage_only") is not True:
        print("FAIL: blotter stage_only is not true")
        return 1

    blotter_run_id = artifact.get("run_id")
    if not blotter_run_id:
        print("FAIL: blotter is missing run_id field")
        return 1

    rows = artifact.get("candidate_rows", [])
    if not isinstance(rows, list):
        print("FAIL: blotter candidate_rows is not a list")
        return 1

    blotter_sha256 = _file_sha256(blotter_path)

    print()
    print(f"Blotter run ID : {blotter_run_id}")
    print(f"Strategy       : {artifact.get('strategy_id', 'unknown')}")
    print(f"Trading date   : {artifact.get('trading_date', 'unknown')}")
    print(f"SHA-256        : {blotter_sha256}")
    print(f"Candidates     : {len(rows)}")
    print()
    print("Candidate orders:")
    _display_candidates(rows)
    print()

    if args.dry_run:
        print("Dry-run: no database write performed.")
        return 0

    try:
        selected_seqs = _resolve_selected_ids(args.order_ids, rows)
    except SystemExit as exc:
        print(str(exc))
        return 1

    n_selected = len(selected_seqs)
    print(
        f"You are about to approve {n_selected} of {len(rows)} candidate orders "
        f"from blotter {blotter_run_id!r} for submission to IBKR paper (port 7497)."
    )
    print()
    answer = input("Type YES to confirm approval: ").strip()
    if answer != "YES":
        print("Approval cancelled (did not receive literal YES).")
        return 1

    operator = (
        args.operator
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "operator"
    )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("FAIL: DATABASE_URL is not set.")
        return 1

    try:
        engine = create_engine(database_url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO blotter_approvals "
                    "(id, blotter_run_id, blotter_local_path, blotter_sha256, "
                    "selected_order_ids, approved_at_utc, approved_by, "
                    "confirmed_blotter_sha256) "
                    "VALUES (:id, :run_id, :local_path, :sha256, "
                    ":selected_ids, :approved_at, :approved_by, :confirmed_sha256)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "run_id": blotter_run_id,
                    "local_path": str(blotter_path),
                    "sha256": blotter_sha256,
                    "selected_ids": json.dumps(selected_seqs),
                    "approved_at": datetime.now(UTC).isoformat(),
                    "approved_by": operator,
                    "confirmed_sha256": blotter_sha256,
                },
            )
        engine.dispose()
    except Exception as exc:
        print(f"FAIL: database error: {exc}")
        return 1

    print()
    print(f"Approved {n_selected} orders for blotter {blotter_run_id!r}.")
    print(f"Operator: {operator}")
    print(f"SHA-256 recorded: {blotter_sha256}")
    print()
    print("The daily_paper_trading DAG will proceed with submission on its next sensor poke.")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

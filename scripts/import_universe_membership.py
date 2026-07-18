"""Operator-run import of point-in-time S&P 500 constituent history (BUG-008).

This is a deliberate manual step, not something run automatically by any
Airflow DAG or pytest suite (the Wikipedia provider performs live network
I/O). Re-run this periodically to advance the validated coverage window —
``load_universe_as_of`` fails closed for any date after the latest published
import's ``coverage_end``.

Usage:
    # Real Wikipedia import (writes to DATABASE_URL, saves raw HTML under
    # data/vendor/):
    python -m scripts.import_universe_membership --coverage-start 1976-07-01

    # Fixture import, for local testing against a throwaway DB:
    python -m scripts.import_universe_membership --provider fixture \\
        --coverage-start 2019-06-01 --database-url sqlite:///local/fixture_universe.db
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine

from data.universe.import_pipeline import ImportValidationError, run_import
from data.universe.providers.fixture_provider import FixtureSP500Provider
from data.universe.providers.wikipedia_sp500 import WikipediaSP500Provider

load_dotenv()

_DEFAULT_ARTIFACT_ROOT = Path("data/vendor")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import point-in-time S&P 500 constituent history.")
    p.add_argument(
        "--provider",
        choices=["wikipedia", "fixture"],
        default="wikipedia",
        help="Constituent-source provider. 'fixture' is for local testing only — "
        "never publishes to the real 'sp500' universe_id.",
    )
    p.add_argument(
        "--coverage-start",
        required=True,
        help="Earliest date the import certifies membership for (YYYY-MM-DD). "
        "For the Wikipedia provider this should not predate 1976-07-01 (the "
        "'Selected changes' table's earliest row).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy connection string. Defaults to DATABASE_URL env var.",
    )
    p.add_argument(
        "--artifact-root",
        default=str(_DEFAULT_ARTIFACT_ROOT),
        help="Directory raw source snapshots are saved under (default: data/vendor).",
    )
    p.add_argument(
        "--snapshot",
        default=None,
        help="Path to a previously saved raw.html snapshot (wikipedia provider only). "
        "When set, no network I/O is performed; the checked-in artifact is imported "
        "reproducibly after checksum verification against its manifest.json.",
    )
    p.add_argument(
        "--exclude-tickers",
        default="",
        help="Comma-separated tickers to exclude entirely from the import (operator "
        "escape hatch for known source ticker-collision rows the validator rejects; "
        "excluded tickers are never eligible historically — fail closed). Record "
        "every exclusion in docs/plans/01b2-constituent-source-contract.md.",
    )
    p.add_argument(
        "--exclude-reason",
        default=None,
        help="Reason recorded in the DB audit record alongside --exclude-tickers.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    coverage_start = date.fromisoformat(args.coverage_start)

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set and --database-url was not passed.")
    engine = create_engine(database_url)

    if args.provider == "fixture":
        provider = FixtureSP500Provider()
    else:
        snapshot = Path(args.snapshot) if args.snapshot else None
        provider = WikipediaSP500Provider(snapshot_path=snapshot)

    exclude = {t.strip().upper() for t in args.exclude_tickers.split(",") if t.strip()}

    try:
        batch = run_import(
            provider,
            engine=engine,
            artifact_root=Path(args.artifact_root),
            coverage_start=coverage_start,
            exclude_tickers=exclude or None,
            exclude_reason=args.exclude_reason,
        )
    except ImportValidationError as exc:
        print("Import REJECTED. Issues:")
        for issue in exc.issues:
            print(f"  - {issue}")
        raise SystemExit(1) from exc

    print(
        f"Import published: universe_id={batch.universe_id!r} batch_id={batch.id} "
        f"coverage=[{batch.coverage_start}, {batch.coverage_end}] "
        f"n_membership_rows={batch.n_membership_rows} "
        f"n_symbol_history_rows={batch.n_symbol_history_rows}"
    )


if __name__ == "__main__":
    main()

"""Hand-curated security_type historical backfill (Roadmap 03A-4b, Phase B
of BUG-078, design doc §5.1/§6 item 3).

Reads a curation source YAML (default:
data/vendor/security_type_curation/sp500_security_types.yaml) and writes one
new universe_eligibility_batches + universe_eligibility_attributes rows: a
default classification for every currently-tracked member with no explicit
curation entry, plus the curated entries themselves for any ticker that has
them.

Usage
-----
    # Dry run — shows what would be written, writes nothing:
    python -m scripts.import_security_type_curation --universe-id sp500 --dry-run

    # Live run:
    python -m scripts.import_security_type_curation --universe-id sp500

Environment variables required (live runs only): DATABASE_URL.
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional

import structlog
import yaml
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

logger = structlog.get_logger(__name__)

load_dotenv()

_DEFAULT_CURATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "vendor"
    / "security_type_curation"
    / "sp500_security_types.yaml"
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import hand-curated security_type history.")
    p.add_argument("--universe-id", default="sp500", help="Universe to backfill security_type for.")
    p.add_argument(
        "--curation-file",
        default=str(_DEFAULT_CURATION_PATH),
        help="Path to the curation source YAML.",
    )
    p.add_argument("--code-version", default=None, help="Provenance tag (default: git short SHA).")
    p.add_argument("--notes", default=None, help="Free-text note stored on the batch row.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print summary statistics but do not write to DB.",
    )
    return p.parse_args()


def _default_code_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:  # pragma: no cover
        return "unknown"


def load_curation_file(path: str) -> tuple[str, list]:
    from data.universe.eligibility_batch import SecurityTypeCurationEntry

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    default_security_type = raw.get("default_security_type", "CS")
    entries = []
    for e in raw.get("entries") or []:
        entries.append(
            SecurityTypeCurationEntry(
                ticker=e["ticker"],
                security_type=e["security_type"],
                effective_start=date.fromisoformat(e["effective_start"]),
                effective_end=(
                    date.fromisoformat(e["effective_end"]) if e.get("effective_end") else None
                ),
                note=e.get("note", ""),
            )
        )
    return default_security_type, entries


def run(
    universe_id: str,
    curation_file: str,
    dry_run: bool,
    code_version: Optional[str] = None,
    notes: Optional[str] = None,
    engine: Optional[Engine] = None,  # injectable for testing
) -> None:
    from data.universe.eligibility_batch import (
        EmptyBatchError,
        load_membership_intervals,
        build_security_type_rows,
        write_security_type_batch,
    )

    if engine is None:
        import os

        engine = create_engine(os.environ["DATABASE_URL"])

    default_security_type, entries = load_curation_file(curation_file)

    if dry_run:
        # Codex-review-adjacent P2 fix (03A-4b PR #42 review): mirror
        # write_security_type_batch's fail-closed EmptyBatchError checks here
        # too, so a preview cannot report "0 rows, success" for an input
        # (e.g. no published universe_membership import yet) that a live run
        # would refuse to write.
        membership_intervals = load_membership_intervals(engine, universe_id)
        if not membership_intervals:
            raise EmptyBatchError(
                f"[DRY RUN] No published universe_membership rows found for "
                f"universe_id={universe_id!r}; a live run would raise the same "
                "error. Run scripts/import_universe_membership.py first."
            )
        rows = build_security_type_rows(
            membership_intervals, entries, default_security_type=default_security_type
        )
        if not rows:
            raise EmptyBatchError(
                f"[DRY RUN] build_security_type_rows produced zero rows for "
                f"universe_id={universe_id!r}. A live run would raise the same error."
            )
        n_curated_tickers = len({e.ticker for e in entries})
        n_default_tickers = len({r["ticker"] for r in rows}) - n_curated_tickers
        print(
            f"[DRY RUN] Would write {len(rows):,} security_type rows: "
            f"{n_curated_tickers:,} hand-curated tickers, {n_default_tickers:,} "
            f"default-classified ({default_security_type}) tickers, for "
            f"universe_id={universe_id!r}."
        )
        return

    result = write_security_type_batch(
        engine,
        universe_id,
        entries,
        code_version=code_version or _default_code_version(),
        default_security_type=default_security_type,
        notes=notes,
    )
    logger.info(
        "security_type_curation_complete",
        universe_id=universe_id,
        batch_id=result.batch_id,
        n_rows_written=result.n_rows_written,
        n_tickers=result.n_tickers,
    )
    print(
        f"Wrote batch_id={result.batch_id} with {result.n_rows_written:,} "
        f"security_type rows across {result.n_tickers:,} tickers."
    )


def main() -> None:
    args = _parse_args()
    run(
        universe_id=args.universe_id,
        curation_file=args.curation_file,
        dry_run=args.dry_run,
        code_version=args.code_version,
        notes=args.notes,
    )


if __name__ == "__main__":
    main()

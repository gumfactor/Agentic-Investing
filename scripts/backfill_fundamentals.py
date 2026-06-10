"""Backfill SEC EDGAR fundamentals for the full price universe.

Fetches XBRL Company Facts for every ticker in daily_prices and writes
rows to financial_statements.  Designed to be interruptible and resumable:
already-processed tickers are skipped on re-run.

Usage
-----
    python scripts/backfill_fundamentals.py [--tickers AAPL MSFT ...] [--force]

Options
-------
  --tickers   Process only these tickers (default: all tickers in daily_prices)
  --force     Re-process tickers that already have rows in financial_statements

Estimated runtime
-----------------
503 tickers × ~3 EDGAR requests each × 0.13 s/request ≈ 3–4 minutes.
EDGAR will return 404 for foreign-listed tickers; those are silently skipped.

Environment
-----------
Requires DATABASE_URL.  Optional EDGAR_USER_AGENT overrides the User-Agent
header sent to SEC (default uses the RQIS app name + operator email).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

import structlog

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # dotenv not installed; DATABASE_URL must be set in environment directly
from sqlalchemy import create_engine, text

logger = structlog.get_logger(__name__)


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _get_universe(engine) -> list[str]:
    """Return all distinct tickers from daily_prices, sorted."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker"))
        return [r[0] for r in rows]


def _already_ingested(engine) -> set[str]:
    """Return the set of tickers that already have rows in financial_statements."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT DISTINCT ticker FROM financial_statements"))
        return {r[0] for r in rows}


def _write_rows(engine, rows: list[dict]) -> int:
    """Upsert fundamental rows into financial_statements.

    The unique key is (ticker, period_end_date, release_date, period_type,
    item_name, source).  On conflict we update value only — all other columns
    are part of the key.
    """
    if not rows:
        return 0

    upsert_sql = text(
        "INSERT INTO financial_statements "
        "(ticker, period_end_date, release_date, period_type, item_name, value, "
        "source, source_version) "
        "VALUES (:ticker, :period_end_date, :release_date, :period_type, "
        ":item_name, :value, :source, :source_version) "
        "ON CONFLICT (ticker, period_end_date, release_date, period_type, item_name, source) "
        "DO UPDATE SET value = EXCLUDED.value, "
        "source_version = EXCLUDED.source_version, ingested_at = NOW()"
    )

    with engine.begin() as conn:
        conn.execute(upsert_sql, rows)

    return len(rows)


# ─── Progress tracking ────────────────────────────────────────────────────────

def _print_progress(done: int, total: int, succeeded: int, failed: int, skipped: int) -> None:
    pct = 100.0 * done / total if total else 0
    print(
        f"\r[{done:4d}/{total}] {pct:5.1f}%  "
        f"ok={succeeded}  fail={failed}  skip={skipped}",
        end="",
        flush=True,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickers", nargs="+", metavar="TICKER", help="Process only these tickers")
    parser.add_argument("--force", action="store_true", help="Re-process tickers already in financial_statements")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    engine = create_engine(database_url)

    # Resolve target tickers
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
        logger.info("backfill_target_from_args", count=len(tickers))
    else:
        tickers = _get_universe(engine)
        logger.info("backfill_universe_loaded", count=len(tickers))

    # Skip already-ingested unless --force
    if not args.force:
        already_done = _already_ingested(engine)
        before = len(tickers)
        tickers = [t for t in tickers if t not in already_done]
        skipped_upfront = before - len(tickers)
        if skipped_upfront:
            logger.info("backfill_skipping_existing", skipped=skipped_upfront, remaining=len(tickers))
    else:
        skipped_upfront = 0

    if not tickers:
        print("All tickers already ingested. Use --force to re-process.")
        return 0

    total = len(tickers)
    print(f"Starting EDGAR backfill: {total} tickers (skipped {skipped_upfront} already ingested)")
    print("Press Ctrl-C to interrupt; re-run without --force to resume.\n")

    # Build EdgarClient — picks up EDGAR_USER_AGENT from env if set
    from data.ingestion.fundamentals.edgar_client import EdgarClient
    user_agent = os.environ.get("EDGAR_USER_AGENT", "RQIS-backfill contact@rqis.internal")
    client = EdgarClient(user_agent=user_agent)

    # Fetch CIK map once
    print("Fetching SEC CIK map...")
    cik_map = client.get_cik_map()
    print(f"CIK map loaded: {len(cik_map)} entries\n")

    succeeded = 0
    failed = 0
    skipped = 0
    total_rows = 0
    start_time = time.monotonic()

    try:
        for i, ticker in enumerate(tickers):
            _print_progress(i, total, succeeded, failed, skipped)

            cik = cik_map.get(ticker.upper())
            if cik is None:
                logger.debug("backfill_no_cik", ticker=ticker)
                skipped += 1
                continue

            try:
                import requests as req_module
                facts = client.fetch_company_facts(cik)
                rows = client.extract_fundamentals(ticker, facts)
                if rows:
                    written = _write_rows(engine, rows)
                    total_rows += written
                    succeeded += 1
                else:
                    skipped += 1
            except req_module.HTTPError as exc:
                status = exc.response.status_code if hasattr(exc, "response") and exc.response is not None else "?"
                if status in (404, 403):
                    # Foreign listing or company not in EDGAR — expected
                    logger.debug("backfill_not_in_edgar", ticker=ticker, status=status)
                    skipped += 1
                else:
                    logger.warning("backfill_http_error", ticker=ticker, status=status, error=str(exc))
                    failed += 1
            except Exception as exc:
                logger.error("backfill_unexpected_error", ticker=ticker, error=str(exc))
                failed += 1

    except KeyboardInterrupt:
        print("\n\nInterrupted. Progress is saved; re-run without --force to continue.")

    _print_progress(min((i + 1) if "i" in locals() else 0, total), total, succeeded, failed, skipped)
    print()  # newline after progress line

    elapsed = time.monotonic() - start_time
    print(
        f"\n{'-' * 50}\n"
        f"Backfill complete in {elapsed:.0f}s\n"
        f"  Succeeded : {succeeded}\n"
        f"  Skipped   : {skipped + skipped_upfront}\n"
        f"  Failed    : {failed}\n"
        f"  Rows written: {total_rows:,}\n"
    )

    if failed > 0:
        logger.warning("backfill_finished_with_failures", failed=failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

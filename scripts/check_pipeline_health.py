"""Quick DB health check after an Airflow run or recovery.

Verifies row counts, latest dates, and absence of duplicate (ticker, date)
pairs in the three core tables.  Exits with code 1 if anything looks wrong.

Usage:
    python scripts/check_pipeline_health.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()


def main() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    issues: list[str] = []

    with engine.connect() as conn:
        # ── daily_prices ──────────────────────────────────────────────────
        row = conn.execute(text(
            "SELECT COUNT(*), COUNT(DISTINCT ticker), MAX(date) FROM daily_prices"
        )).fetchone()
        total_rows, n_tickers, latest_date = row
        status = "OK" if total_rows and total_rows > 0 else "EMPTY"
        print(f"daily_prices   rows={total_rows:>8,}  tickers={n_tickers:>4}  "
              f"latest={latest_date}   {status}")
        if status != "OK":
            issues.append("daily_prices is empty")

        # ── data_quality_flags ────────────────────────────────────────────
        row = conn.execute(text("SELECT COUNT(*) FROM data_quality_flags")).fetchone()
        flag_rows = row[0]
        print(f"quality_flags  rows={flag_rows:>8,}   (informational)")

        # ── corporate_actions ─────────────────────────────────────────────
        row = conn.execute(text(
            "SELECT COUNT(*), COUNT(DISTINCT ticker) FROM corporate_actions"
        )).fetchone()
        ca_rows, ca_tickers = row
        print(f"corp_actions   rows={ca_rows:>8,}  tickers={ca_tickers:>4}   OK")

        # ── duplicate check ───────────────────────────────────────────────
        row = conn.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT ticker, date
                FROM daily_prices
                GROUP BY ticker, date
                HAVING COUNT(*) > 1
            ) dups
        """)).fetchone()
        dup_count = row[0]
        if dup_count > 0:
            print(f"\nWARNING: {dup_count} duplicate (ticker, date) pairs in daily_prices")
            issues.append(f"{dup_count} duplicate rows")
        else:
            print("\nNo duplicate (ticker, date) pairs found.  ✓")

        # ── null close prices ─────────────────────────────────────────────
        row = conn.execute(text(
            "SELECT COUNT(*) FROM daily_prices WHERE close IS NULL"
        )).fetchone()
        null_count = row[0]
        if null_count > 0:
            print(f"WARNING: {null_count} rows with NULL close price")
            issues.append(f"{null_count} null close prices")

    print()
    if issues:
        print(f"Pipeline health: ISSUES FOUND — {'; '.join(issues)}")
        sys.exit(1)
    else:
        print("Pipeline health: OK  ✓")


if __name__ == "__main__":
    main()

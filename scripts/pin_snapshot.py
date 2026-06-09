"""Pin the current daily_prices table as a versioned parquet snapshot in MinIO.

Run this after a successful backfill to record the Phase 1 dataset version.
The returned path should be stored as the data_version in any MLflow backtest
run that uses this data (C7).

Usage:
    python scripts/pin_snapshot.py
"""

import sys
from pathlib import Path

# Ensure the project root is on sys.path when the script is run directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from datetime import date
import os

import pandas as pd
from sqlalchemy import create_engine

from data.storage.parquet_snapshots import ParquetSnapshots


def main() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])

    print("Reading daily_prices from TimescaleDB...")
    df = pd.read_sql("SELECT * FROM daily_prices ORDER BY ticker, date", engine)
    print(f"  {len(df):,} rows, {df['ticker'].nunique()} tickers")

    snap = ParquetSnapshots()
    path = snap.save_snapshot(df, data_type="daily_prices", snapshot_date=date.today())
    print(f"\nPhase 1 dataset pinned at: {path}")
    print("Store this path as data_version in MLflow when running backtests.")


if __name__ == "__main__":
    main()

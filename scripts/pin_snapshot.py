"""Pin a complete, versioned backtest dataset bundle in MinIO.

The bundle contains daily prices, strategy-specific alpha scores, corporate
actions, and benchmark prices under one snapshot date. A manifest records the
object paths, row counts, date ranges, schema hashes, and producing git commit.

Usage:
    python -m scripts.pin_snapshot --strategy-id v1 --benchmark SPY
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from backtesting.dataset_manifest import build_manifest
from data.ingestion.market.yfinance_client import YFinanceClient
from data.storage.parquet_snapshots import ParquetSnapshots

load_dotenv()

_YFINANCE_CACHE_DIR = Path(
    os.environ.get("YFINANCE_CACHE_DIR", ".yfinance-cache")
).resolve()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pin a complete backtest dataset bundle.")
    parser.add_argument("--strategy-id", required=True, help="Strategy ID to include.")
    parser.add_argument("--benchmark", default="SPY", help="Benchmark ticker (default: SPY).")
    parser.add_argument(
        "--snapshot-date",
        default=str(date.today()),
        help="Bundle version date (YYYY-MM-DD; default: today).",
    )
    return parser.parse_args()


def pin_bundle(
    strategy_id: str,
    benchmark_ticker: str,
    snapshot_date: date,
    *,
    engine=None,
    snapshots=None,
    market_client=None,
) -> str:
    """Build and save a complete backtest bundle, returning its manifest path."""
    engine = engine or create_engine(os.environ["DATABASE_URL"])
    snapshots = snapshots or ParquetSnapshots()
    market_client = market_client or YFinanceClient(batch_size=1, inter_batch_delay=0)

    prices = pd.read_sql(
        "SELECT * FROM daily_prices ORDER BY ticker, date",
        engine,
    )
    alpha_scores = pd.read_sql(
        text(
            """
            SELECT *
            FROM alpha_scores
            WHERE strategy_id = :strategy_id
            ORDER BY score_date, ticker
            """
        ),
        engine,
        params={"strategy_id": strategy_id},
    )
    corporate_actions = pd.read_sql(
        "SELECT * FROM corporate_actions ORDER BY ticker, ex_date",
        engine,
    )

    if prices.empty:
        raise ValueError("daily_prices is empty; cannot pin a backtest bundle")
    if alpha_scores.empty:
        raise ValueError(f"No alpha_scores found for strategy_id={strategy_id!r}")

    price_start = pd.to_datetime(prices["date"]).min().date()
    price_end = pd.to_datetime(prices["date"]).max().date()
    benchmark, _ = market_client.fetch_market_data(
        [benchmark_ticker],
        start=price_start,
        end=price_end,
    )
    if benchmark.empty:
        raise ValueError(
            f"No benchmark data returned for {benchmark_ticker} "
            f"between {price_start} and {price_end}"
        )

    benchmark_dates = pd.to_datetime(benchmark["date"])
    alpha_start = pd.to_datetime(alpha_scores["score_date"]).min()
    alpha_end = pd.to_datetime(alpha_scores["score_date"]).max()
    if benchmark_dates.min() > alpha_start or benchmark_dates.max() < alpha_end:
        raise ValueError(
            f"Benchmark coverage {benchmark_dates.min().date()} to "
            f"{benchmark_dates.max().date()} does not cover alpha scores "
            f"{alpha_start.date()} to {alpha_end.date()}"
        )

    dataframes = {
        "daily_prices": prices,
        "alpha_scores": alpha_scores,
        "corporate_actions": corporate_actions,
        "benchmark": benchmark,
    }
    object_paths = {
        data_type: snapshots.save_snapshot(
            df,
            data_type=data_type,
            snapshot_date=snapshot_date,
        )
        for data_type, df in dataframes.items()
    }
    snapshot_dates = {data_type: snapshot_date for data_type in dataframes}
    manifest = build_manifest(
        version=str(snapshot_date),
        strategy_id=strategy_id,
        dataframes=dataframes,
        object_paths=object_paths,
        snapshot_dates=snapshot_dates,
    )
    return snapshots.save_dataset_manifest(manifest)


def main() -> None:
    args = _parse_args()
    _YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(_YFINANCE_CACHE_DIR))
    manifest_path = pin_bundle(
        strategy_id=args.strategy_id,
        benchmark_ticker=args.benchmark,
        snapshot_date=date.fromisoformat(args.snapshot_date),
    )
    print(f"Backtest dataset bundle pinned: {manifest_path}")


if __name__ == "__main__":
    main()

"""Pin a complete, versioned backtest dataset bundle in MinIO.

The bundle contains daily prices, strategy-specific alpha scores, corporate
actions, and benchmark prices. Each dataframe is written to a content-
addressed object (03A-1 -- BUG-038): ParquetSnapshots.save_snapshot keys the
object by the canonical LOGICAL content hash of the dataframe, not the
caller-supplied `--snapshot-date`, so re-running this script against
unchanged source data is a safe no-op that writes zero new MinIO objects.
`--snapshot-date` remains only as a human-readable label recorded on the
manifest, not as any object's identity.

The manifest records the object paths, row counts, date ranges, schema
hashes, per-data-type content hashes, and producing git commit, and is itself
stored at a content-addressed key
(manifests/{manifest_content_sha256}/manifest.json). Use
`manifest.manifest_content_sha256` -- printed below -- as the MLflow
data_version (C7); it is a single opaque, verifiable token, not a mutable-
looking date string.

Usage:
    python -m scripts.pin_snapshot --strategy-id v1 --benchmark SPY
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from backtesting.dataset_manifest import build_manifest

load_dotenv()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pin a complete backtest dataset bundle.")
    parser.add_argument("--strategy-id", required=True, help="Strategy ID to include.")
    parser.add_argument("--benchmark", default="SPY", help="Benchmark ticker (default: SPY).")
    parser.add_argument(
        "--snapshot-date",
        default=str(date.today()),
        help="Bundle version date (YYYY-MM-DD; default: today).",
    )
    parser.add_argument(
        "--research-run-id",
        type=int,
        default=None,
        help="Pin alpha_scores only from this research_runs.id (BUG-009 section 4). "
        "Optional when a strategy's whole alpha_scores history was scored under "
        "exactly one research_run_id. If it spans more than one run -- whether "
        "colliding on the SAME (ticker, score_date), or merely covering disjoint "
        "date ranges (adversarial-review round 11: even disjoint ranges splice "
        "methodologically distinct score series into one bundle) -- pin_bundle() "
        "rejects the unsafe case instead of silently pinning a mix. Pass this to "
        "disambiguate, or to pin a specific run's rows only.",
    )
    return parser.parse_args()


def pin_bundle(
    strategy_id: str,
    benchmark_ticker: str,
    snapshot_date: date,
    *,
    research_run_id: int | None = None,
    engine=None,
    snapshots=None,
    market_client=None,
) -> str:
    """Build and save a complete backtest bundle, returning its manifest path."""
    engine = engine or create_engine(os.environ["DATABASE_URL"])
    if snapshots is None:
        from data.storage.parquet_snapshots import ParquetSnapshots  # lazy: pulls in minio
        snapshots = ParquetSnapshots()
    if market_client is None:
        from data.ingestion.market.yfinance_client import YFinanceClient  # lazy: pulls in yfinance
        market_client = YFinanceClient(batch_size=1, inter_batch_delay=0)

    prices = pd.read_sql(
        "SELECT * FROM daily_prices ORDER BY ticker, date",
        engine,
    )
    if research_run_id is not None:
        alpha_scores = pd.read_sql(
            text(
                """
                SELECT *
                FROM alpha_scores
                WHERE strategy_id = :strategy_id AND research_run_id = :research_run_id
                ORDER BY score_date, ticker
                """
            ),
            engine,
            params={"strategy_id": strategy_id, "research_run_id": research_run_id},
        )
    else:
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

    # BUG-009 section 4 (adversarial review round 3, widened round 11):
    # research_run_id is part of alpha_scores' identity now, so a strategy
    # backfilled more than once can legitimately have rows from several
    # runs. Round 3 only rejected the SAME (ticker, score_date) pair
    # spanning more than one run (duplicate cross-section). Round 11 found
    # that check alone is not sufficient: two runs under DIFFERENT
    # methodologies (e.g. legacy same-close/current-membership scores and
    # new t+1/PIT scores) can cover entirely DISJOINT date ranges with zero
    # (ticker, score_date) collisions, so the round-3 check passes silently
    # -- yet pinning both still splices two methodologically incompatible
    # score series into one backtest bundle, treated by every downstream
    # consumer as one coherent series. Require the returned alpha_scores to
    # come from EXACTLY ONE research_run_id when no run is explicitly
    # requested, not merely from non-colliding (ticker, score_date) pairs.
    # --research-run-id remains the explicit, single-run opt-in that always
    # bypasses this (there is nothing left to splice once scoped to one
    # run).
    if research_run_id is None and "research_run_id" in alpha_scores.columns:
        distinct_runs = sorted(alpha_scores["research_run_id"].dropna().unique().tolist())
        if len(distinct_runs) > 1:
            dup_key = alpha_scores.groupby(["ticker", "score_date"])["research_run_id"].nunique()
            colliding = dup_key[dup_key > 1]
            if not colliding.empty:
                sample = list(colliding.index[:5])
                raise ValueError(
                    f"alpha_scores for strategy_id={strategy_id!r} has {len(colliding)} "
                    f"(ticker, score_date) pairs spanning more than one research_run_id "
                    f"(e.g. {sample}). Pinning all of them would duplicate that "
                    "cross-section in the bundle. Pass --research-run-id to select the "
                    "run whose rows should be pinned."
                )
            raise ValueError(
                f"alpha_scores for strategy_id={strategy_id!r} spans "
                f"{len(distinct_runs)} distinct research_run_ids ({distinct_runs}) "
                "with no overlapping (ticker, score_date) pairs between them -- "
                "each run individually looks clean, but pinning all of them "
                "together would splice methodologically distinct score series "
                "(e.g. legacy same-close/current-membership scores alongside "
                "new t+1/PIT scores) into one bundle that every downstream "
                "consumer treats as a single coherent series (BUG-009 section "
                "4, adversarial-review round 11). Pass --research-run-id to "
                "pin exactly one run's rows."
            )

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
    bytes_hashes: dict[str, str] = {}
    object_paths = {
        data_type: snapshots.save_snapshot(
            df,
            data_type=data_type,
            snapshot_date=snapshot_date,
            bytes_sha256_out=bytes_hashes,
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
        bytes_sha256=bytes_hashes,
    )
    return snapshots.save_dataset_manifest(manifest)


def main() -> None:
    import yfinance as yf

    args = _parse_args()
    yfinance_cache_dir = Path(
        os.environ.get("YFINANCE_CACHE_DIR", ".yfinance-cache")
    ).resolve()
    yfinance_cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(yfinance_cache_dir))
    manifest_path = pin_bundle(
        strategy_id=args.strategy_id,
        benchmark_ticker=args.benchmark,
        snapshot_date=date.fromisoformat(args.snapshot_date),
        research_run_id=args.research_run_id,
    )
    print(f"Backtest dataset bundle pinned: {manifest_path}")


if __name__ == "__main__":
    main()

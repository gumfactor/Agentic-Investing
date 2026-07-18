"""Backfill momentum alpha scores for a historical date range.

Problem addressed (Codex finding #7)
--------------------------------------
The Airflow DAG that scores momentum factors started running on 2026-06-09.
Any backtest covering 2020–2024 requires signals for that period, which the
DAG cannot retroactively supply.  This script generates those signals from a
pinned price snapshot and writes them to the alpha_scores (and factor_scores)
tables.

Point-in-time safety
---------------------
compute_momentum_scores() processes the full price history and computes each
date's score from only the trailing window ending on that date.  No forward
prices enter any score.  The price snapshot must therefore cover at least
252 + 21 trading days before the desired start date (the 12-month window plus
the skip buffer).  The recommended practice is to load a snapshot that begins
at least 18 months before --start.

Point-in-time universe (BUG-008 / 01B-2)
----------------------------------------
This is a HISTORICAL caller: by default it requires a published point-in-time
universe import (scripts/import_universe_membership.py) and filters every
score date's cross-section to tickers with knowable index membership on that
date.  It fails closed when no published import exists or when any score date
falls outside the validated coverage window.  --provisional-no-universe
skips the membership filter with a loud warning; the resulting scores are
PROVISIONAL and must not be used for selection, promotion, or paper-trading
qualification.

Usage
------
    # Dry run — shows what would be written, writes nothing:
    python -m scripts.backfill_momentum_scores \\
        --snapshot-date 2026-06-10 \\
        --start 2020-01-02 --end 2024-12-31 \\
        --strategy-id v1 --dry-run

    # Live run:
    python -m scripts.backfill_momentum_scores \\
        --snapshot-date 2026-06-10 \\
        --start 2020-01-02 --end 2024-12-31 \\
        --strategy-id v1

Environment variables required:
    MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY - MinIO credentials
    DATABASE_URL - TimescaleDB connection string (live runs only)
"""

from __future__ import annotations

import argparse
from datetime import date
from typing import Optional

import pandas as pd
import structlog
from dotenv import load_dotenv

logger = structlog.get_logger(__name__)

load_dotenv()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill momentum alpha scores.")
    p.add_argument(
        "--snapshot-date",
        required=True,
        help="MinIO snapshot date to load prices from (YYYY-MM-DD).",
    )
    p.add_argument("--start", required=True, help="First score_date to generate (YYYY-MM-DD).")
    p.add_argument("--end", required=True, help="Last score_date to generate (YYYY-MM-DD).")
    p.add_argument("--strategy-id", default="v1", help="strategy_id tag written to DB.")
    p.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Number of score_dates processed per DB write batch (default 20).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print summary statistics but do not write to DB.",
    )
    p.add_argument(
        "--universe-id",
        default="sp500",
        help="Point-in-time universe to filter membership against (default: sp500).",
    )
    p.add_argument(
        "--provisional-no-universe",
        action="store_true",
        help="Skip point-in-time membership filtering (BUG-008). The resulting "
        "scores are PROVISIONAL: not valid for selection, promotion, or "
        "paper-trading qualification.",
    )
    p.add_argument(
        "--research-run-id",
        type=int,
        required=True,
        help="research_runs.id (BUG-009 section 4 / migration 012) tagging every "
        "written factor_scores/alpha_scores row with the methodology that "
        "produced it. Register one first with "
        "data.research.identity.register_methodology/register_run — required "
        "so a new backfill can never silently overwrite an old methodology's "
        "rows via the ON CONFLICT upsert.",
    )
    return p.parse_args()


def run(
    snapshot_date: date,
    start: date,
    end: date,
    strategy_id: str,
    batch_size: int,
    dry_run: bool,
    research_run_id: Optional[int] = None,
    snapshots=None,  # injectable for testing; None → construct from env vars
    universe_id: str = "sp500",
    provisional_no_universe: bool = False,
    universe_lookup=None,  # injectable for testing; None → construct from DATABASE_URL
) -> None:
    from data.storage.timescale_writer import TimescaleWriter
    from signals.composites.momentum_score import compute_momentum_scores
    from signals.scoring.scorer import combine_factor_scores

    if snapshots is None:
        from data.storage.parquet_snapshots import ParquetSnapshots
        snapshots = ParquetSnapshots()
    snaps = snapshots

    # ── Load price snapshot ───────────────────────────────────────────────────
    logger.info("loading_price_snapshot", snapshot_date=str(snapshot_date))
    prices = snaps.load_snapshot("daily_prices", snapshot_date)

    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    # ── Hard guard: require sufficient lookback before --start ────────────────
    # The 12-month momentum window uses 252 trading-day rows; the skip buffer
    # adds another 21.  A calendar-day proxy is imprecise because market
    # holidays vary by year.  Count actual distinct trading dates in the
    # snapshot that fall strictly before --start instead.
    _MIN_LOOKBACK_DAYS = 252 + 21  # rows, not calendar days
    lookback_days = prices[prices["date"] < start]["date"].nunique()
    if lookback_days < _MIN_LOOKBACK_DAYS:
        raise ValueError(
            f"Insufficient price history: the snapshot has {lookback_days} trading "
            f"days before --start {start}, but the 12-month momentum window requires "
            f"at least {_MIN_LOOKBACK_DAYS}. Either extend the snapshot to cover an "
            f"earlier start date or move --start later."
        )

    # ── Point-in-time scoring cross-section (BUG-008 / Codex PR #34 P1) ──────
    # Membership must define the cross-section BEFORE z-scoring: filtering
    # only the output rows would leave non-members contaminating each date's
    # cross-sectional mean/std even though their rows are later dropped.
    # Raw window returns still use every ticker's full price history, so
    # lookbacks spanning a ticker's pre-membership period are unaffected.
    eligibility_df = None
    eligible_by_date: dict = {}
    if provisional_no_universe:
        logger.warning(
            "backfill_without_pit_universe",
            note=(
                "membership filtering skipped (--provisional-no-universe); the "
                "resulting scores are PROVISIONAL and must not be used for "
                "selection, promotion, or paper-trading qualification (BUG-008)"
            ),
        )
    else:
        import os

        from data.universe.runtime import PITUniverseLookup

        if universe_lookup is None:
            universe_lookup = PITUniverseLookup(os.environ["DATABASE_URL"], universe_id)
        candidate_dates = sorted(
            d for d in prices["date"].unique() if start <= d <= end
        )
        eligibility_rows: list[dict] = []
        for d in candidate_dates:
            eligible = set(universe_lookup.load_universe_as_of(d).eligible_tickers)
            eligible_by_date[d] = eligible
            eligibility_rows.extend({"ticker": t, "date": d} for t in eligible)
        eligibility_df = pd.DataFrame(eligibility_rows, columns=["ticker", "date"])
        logger.info(
            "pit_scoring_cross_section_built",
            universe_id=universe_lookup.universe_id,
            import_batch_id=universe_lookup.import_batch_id,
            n_score_dates=len(candidate_dates),
        )

    # ── Compute momentum scores for all dates in one vectorised pass ──────────
    logger.info("computing_momentum_scores", n_price_rows=len(prices))
    prices["close"] = prices["close"].astype(float)
    momentum_df = compute_momentum_scores(prices, eligibility=eligibility_df)

    # Keep "date" column name — combine_factor_scores expects "date", not "score_date".
    # The scorer renames the column internally when building the output DataFrames.
    momentum_df["date"] = pd.to_datetime(momentum_df["date"]).dt.date
    mask = (momentum_df["date"] >= start) & (momentum_df["date"] <= end)
    momentum_df = momentum_df[mask].reset_index(drop=True)

    if eligibility_df is not None:
        # Belt-and-braces output filter: with the pre-z-score mask this is a
        # no-op for members, but it guarantees no ineligible (ticker, date)
        # row can ever be persisted.
        n_before = len(momentum_df)
        keep = [
            row.ticker in eligible_by_date.get(row.date, set())
            for row in momentum_df[["ticker", "date"]].itertuples(index=False)
        ]
        momentum_df = momentum_df[pd.Series(keep, index=momentum_df.index)].reset_index(drop=True)
        logger.info(
            "pit_membership_filter_applied",
            rows_before=n_before,
            rows_after=len(momentum_df),
            rows_excluded=n_before - len(momentum_df),
        )

    score_dates = sorted(momentum_df["date"].unique())
    logger.info(
        "scores_computed",
        n_score_dates=len(score_dates),
        n_rows=len(momentum_df),
        first_date=str(score_dates[0]) if score_dates else "none",
        last_date=str(score_dates[-1]) if score_dates else "none",
    )

    if dry_run:
        print(
            f"[DRY RUN] Would write {len(momentum_df):,} factor_score rows "
            f"across {len(score_dates):,} dates "
            f"({start} to {end}, strategy_id={strategy_id!r})."
        )
        print(momentum_df.head(10).to_string(index=False))
        return

    # ── Write to DB in batches ────────────────────────────────────────────────
    if research_run_id is None:
        raise ValueError(
            "research_run_id is required for a non-dry-run write (BUG-009 section "
            "4 / migration 012): register a research_methodologies/research_runs "
            "pair first via data.research.identity so this backfill's rows are "
            "attributable and cannot silently overwrite an old methodology's rows."
        )
    writer = TimescaleWriter()
    total_factor_rows = 0
    total_alpha_rows = 0

    date_batches = [
        score_dates[i : i + batch_size]
        for i in range(0, len(score_dates), batch_size)
    ]
    logger.info("writing_to_db", n_batches=len(date_batches), batch_size=batch_size)

    for batch_idx, date_batch in enumerate(date_batches):
        batch_mask = momentum_df["date"].isin(date_batch)
        batch_df = momentum_df[batch_mask]

        # factor_scores needs score_date column; rename here for the DB write.
        factor_rows = (
            batch_df[["ticker", "date", "momentum_score"]]
            .copy()
            .rename(columns={"date": "score_date", "momentum_score": "z_score"})
        )
        factor_rows["factor_name"] = "momentum"
        factor_rows["strategy_id"] = strategy_id
        factor_rows["research_run_id"] = research_run_id

        # combine_factor_scores expects "date" column — pass the original.
        alpha_frames = []
        for sd in date_batch:
            day_df = batch_df[batch_df["date"] == sd].copy()
            if day_df.empty:
                continue
            _, alpha_df = combine_factor_scores(
                factor_scores={"momentum": day_df[["ticker", "date", "momentum_score"]]},
                score_col_map={"momentum": "momentum_score"},
                strategy_id=strategy_id,
                score_date=sd,
            )
            alpha_frames.append(alpha_df)

        alpha_combined = pd.concat(alpha_frames, ignore_index=True) if alpha_frames else pd.DataFrame()
        if not alpha_combined.empty:
            alpha_combined["research_run_id"] = research_run_id

        n_f = writer.upsert_factor_scores(factor_rows)
        n_a = writer.upsert_alpha_scores(alpha_combined) if not alpha_combined.empty else 0
        total_factor_rows += n_f
        total_alpha_rows += n_a

        if (batch_idx + 1) % 10 == 0 or (batch_idx + 1) == len(date_batches):
            logger.info(
                "batch_progress",
                batch=batch_idx + 1,
                total_batches=len(date_batches),
                factor_rows_so_far=total_factor_rows,
                alpha_rows_so_far=total_alpha_rows,
            )

    logger.info(
        "backfill_complete",
        total_factor_rows=total_factor_rows,
        total_alpha_rows=total_alpha_rows,
        start=str(start),
        end=str(end),
        strategy_id=strategy_id,
    )


def main() -> None:
    args = _parse_args()
    run(
        snapshot_date=date.fromisoformat(args.snapshot_date),
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        strategy_id=args.strategy_id,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        research_run_id=args.research_run_id,
        universe_id=args.universe_id,
        provisional_no_universe=args.provisional_no_universe,
    )


if __name__ == "__main__":
    main()

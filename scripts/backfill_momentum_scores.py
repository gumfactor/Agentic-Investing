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
        default=None,
        help="research_runs.id (BUG-009 section 4 / migration 012) tagging every "
        "written factor_scores/alpha_scores row with the methodology that "
        "produced it. Register one first with "
        "data.research.identity.register_methodology/register_run. Optional "
        "at the CLI level for --dry-run (which never writes); run() still "
        "hard-requires it for an actual (non-dry-run) write so a new "
        "backfill can never silently overwrite an old methodology's rows "
        "via the ON CONFLICT upsert.",
    )
    p.add_argument(
        "--allow-raw-prices-on-missing-actions",
        action="store_true",
        help="Explicit opt-in (adversarial-review round 9, BUG-009): a live "
        "(non-dry-run) write with no corporate_actions snapshot pinned for "
        "--snapshot-date fails closed by default rather than silently "
        "persisting raw, unadjusted momentum scores under a research_run_id "
        "whose methodology claims cutoff-adjustment was applied. Pass this "
        "flag together with --research-run-id pointing at a run whose "
        "methodology honestly declares score_action_availability_policy != "
        "'score_cutoff_known_at_v1' to proceed anyway. --dry-run is always "
        "permissive (preview only, never persists) and does not need this "
        "flag.",
    )
    return p.parse_args()


def _validate_raw_prices_methodology_is_honest(research_run_id: int) -> None:
    """Refuse to persist raw/unadjusted momentum scores under a methodology
    that claims cutoff-adjustment was applied (adversarial-review round 9,
    BUG-009 section 2.3/4): that is the same "provenance lies about what
    actually happened" pattern as the original P0 finding this whole task
    exists to prevent, just reached via a silent degrade (missing
    corporate_actions snapshot) instead of missing wiring. This script is
    not Airflow-reachable, so the ORM (data.research.models/identity) is
    safe to use here unlike the DAG-reachable modules elsewhere in 01B-3.
    """
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from data.research.models import ResearchMethodology, ResearchRun

    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        run_row = session.get(ResearchRun, research_run_id)
        if run_row is None:
            raise ValueError(
                f"--research-run-id={research_run_id} does not exist in research_runs."
            )
        methodology = session.get(ResearchMethodology, run_row.methodology_id)
        if methodology is None:
            raise ValueError(
                f"research_runs.id={research_run_id} references a missing "
                f"methodology_id={run_row.methodology_id}."
            )
        if methodology.score_action_availability_policy == "score_cutoff_known_at_v1":
            raise ValueError(
                f"--research-run-id={research_run_id} is tagged with methodology "
                f"{methodology.name!r}, whose score_action_availability_policy is "
                "'score_cutoff_known_at_v1' -- it claims cutoff-adjusted "
                "corporate-action handling was applied. Writing RAW (unadjusted) "
                "momentum scores under that methodology would misrepresent what "
                "was actually computed (BUG-009). Register a distinct methodology "
                "whose score_action_availability_policy honestly declares no "
                "cutoff adjustment was applied (e.g. "
                "'raw_unadjusted_no_corporate_action_data'), activate a run under "
                "it, and pass that run's id instead."
            )


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
    allow_raw_prices_on_missing_actions: bool = False,
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

    # ── Corporate-action cutoff-aware adjustment (BUG-009 section 2.3) ───────
    # Momentum is a pure price-ratio indicator: its score at date t must be
    # computed from a price history where only actions known-and-occurred by
    # t's cutoff have adjusted the input prices. This backfill computes every
    # score date in one vectorized pass rather than looping per date; using
    # ONE boundary cutoff (session close of --end) rather than a literal
    # per-score-date cutoff is provably equivalent for a window/ratio-based
    # indicator like momentum, because a uniform multiplicative adjustment
    # factor cancels out of any price ratio computed within a lookback window
    # that lies entirely before the action's ex_date (see
    # scripts/validate_signal_ic.py::_build_adjusted_price_series for the
    # full derivation). The one residual gap — an action whose ex_date falls
    # exactly on a given score_date — is documented as BUG-071 in bugs.md.
    from data.normalization.corporate_actions import build_score_price_history_as_of
    from data.universe.calendar import session_close_cutoff

    try:
        corporate_actions = snaps.load_snapshot("corporate_actions", snapshot_date)
    except FileNotFoundError:
        # Adversarial-review round 9 (BUG-009): a missing snapshot used to
        # silently degrade to an empty action set and let the run proceed --
        # writing raw, unadjusted momentum scores tagged with a
        # research_run_id whose registered methodology
        # (score_cutoff_known_at_v1) claims cutoff-adjustment WAS applied.
        # That is the same "provenance lies about what actually happened"
        # pattern as the original P0 finding this task exists to prevent,
        # just reached via a silent degrade instead of missing wiring. A
        # dry run is still permissive (preview only, never persists); a
        # live write fails closed unless the caller explicitly opts in via
        # --allow-raw-prices-on-missing-actions AND supplies a
        # --research-run-id whose methodology honestly declares it did not
        # apply cutoff adjustment (validated below, before any further work).
        if not dry_run:
            if not allow_raw_prices_on_missing_actions:
                raise RuntimeError(
                    f"corporate_actions snapshot is missing for "
                    f"snapshot_date={snapshot_date} and this is a live "
                    "(non-dry-run) write. Proceeding would silently persist raw, "
                    "unadjusted momentum scores under a research_run_id whose "
                    "methodology may claim cutoff-adjusted corporate-action "
                    "handling was applied (BUG-009). Either re-pin a "
                    "corporate_actions snapshot for this snapshot_date "
                    "(scripts/pin_snapshot.py), run with --dry-run to preview "
                    "without persisting, or pass "
                    "--allow-raw-prices-on-missing-actions together with a "
                    "--research-run-id whose methodology honestly declares "
                    "score_action_availability_policy != "
                    "'score_cutoff_known_at_v1'."
                )
            if research_run_id is None:
                raise ValueError(
                    "--allow-raw-prices-on-missing-actions requires "
                    "--research-run-id so the run's methodology can be "
                    "validated for honesty before any scores are computed "
                    "(BUG-009)."
                )
            _validate_raw_prices_methodology_is_honest(research_run_id)

        corporate_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]
        )
        logger.warning(
            "corporate_actions_snapshot_missing",
            snapshot_date=str(snapshot_date),
            note="no corporate_actions snapshot pinned for this snapshot_date; "
            "momentum scores will use raw (unadjusted) prices (BUG-009 section 2.3)",
        )
    else:
        corporate_actions["ex_date"] = pd.to_datetime(corporate_actions["ex_date"]).dt.date

        # BUG-009 P2 (adversarial review round 3): a snapshot pinned before
        # migration 011 (scripts/pin_snapshot.py did `SELECT *` against
        # corporate_actions, so its columns mirror whatever the live table
        # had at pin time) has no known_at/source_version columns at all --
        # every action it contains is, by construction, a legacy yfinance
        # date-only record with no announcement timestamp. Synthesize
        # known_at with the SAME conservative next-session rule migration
        # 011 used to backfill the live table (no earlier than the close of
        # the next trading session after ex_date), rather than raising a
        # bare KeyError or silently skipping adjustment for the whole
        # snapshot. This is provenance-labeled, not silently assumed: every
        # synthesized row is tagged with a distinct source_version so a
        # reader can tell it apart from a genuinely migrated live-table row.
        if "known_at" not in corporate_actions.columns:
            from data.universe.calendar import conservative_known_at_for_date_only_source

            logger.warning(
                "corporate_actions_snapshot_predates_migration_011",
                snapshot_date=str(snapshot_date),
                n_actions=len(corporate_actions),
                note="snapshot has no known_at/source_version columns; synthesizing "
                "known_at via the conservative next-session rule (BUG-009 section 2.3) "
                "-- re-pin the snapshot after migration 011 for a live-table-backed "
                "known_at instead",
            )
            corporate_actions["known_at"] = corporate_actions["ex_date"].apply(
                conservative_known_at_for_date_only_source
            )
            corporate_actions["source_version"] = "legacy_pre_migration_011_snapshot"
        else:
            corporate_actions["known_at"] = pd.to_datetime(corporate_actions["known_at"], utc=True)
            if "source_version" not in corporate_actions.columns:
                corporate_actions["source_version"] = "unknown"

    boundary_cutoff = session_close_cutoff(min(end, prices["date"].max()))
    adjusted_prices, adj_meta = build_score_price_history_as_of(
        prices, corporate_actions, score_cutoff=boundary_cutoff
    )
    logger.info(
        "cutoff_adjusted_price_series_built",
        boundary_cutoff=boundary_cutoff.isoformat(),
        n_actions_considered=adj_meta.n_actions_considered,
        n_actions_excluded_by_cutoff=adj_meta.n_actions_excluded_by_cutoff,
        n_actions_excluded_missing_known_at=adj_meta.n_actions_excluded_missing_known_at,
    )
    prices_for_scoring = adjusted_prices[["ticker", "date", "adj_close"]].rename(
        columns={"adj_close": "close"}
    )
    prices_for_scoring["close"] = prices_for_scoring["close"].astype(float)

    # ── Compute momentum scores for all dates in one vectorised pass ──────────
    logger.info("computing_momentum_scores", n_price_rows=len(prices_for_scoring))
    momentum_df = compute_momentum_scores(prices_for_scoring, eligibility=eligibility_df)

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
        allow_raw_prices_on_missing_actions=args.allow_raw_prices_on_missing_actions,
    )


if __name__ == "__main__":
    main()

"""Daily/historical batch job: populate adv_usd_20d/price_usd PIT eligibility
attributes from daily_prices (Roadmap 03A-4b, Phase B of BUG-078).

Design doc: docs/plans/03a-immutable-research-data-design.md §1.4/§5.1.
market_cap_usd is permanently out of scope (see
data/universe/eligibility_batch.py module docstring) -- do not extend this
script to compute it without a new binding operator decision.

Usage
-----
    # Dry run — shows what would be written, writes nothing:
    python -m scripts.backfill_eligibility_attributes \\
        --universe-id sp500 --start 2022-07-11 --end 2024-12-31 --dry-run

    # Live run (full historical backfill, per design doc §5.1 "run once"):
    python -m scripts.backfill_eligibility_attributes \\
        --universe-id sp500 --start 2022-07-11 --end 2024-12-31

Environment variables required (live runs only): DATABASE_URL.
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import date
from typing import Optional

import pandas as pd
import structlog
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = structlog.get_logger(__name__)

load_dotenv()

DEFAULT_ADV_WINDOW = 20


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill adv_usd_20d/price_usd PIT eligibility attributes."
    )
    p.add_argument("--universe-id", default="sp500", help="Universe to compute attributes for.")
    p.add_argument("--start", required=True, help="First effective_start date (YYYY-MM-DD).")
    p.add_argument("--end", required=True, help="Last effective_start date (YYYY-MM-DD).")
    p.add_argument(
        "--adv-window",
        type=int,
        default=DEFAULT_ADV_WINDOW,
        help=f"Trailing-session dollar-volume average window (default {DEFAULT_ADV_WINDOW}).",
    )
    p.add_argument(
        "--code-version",
        default=None,
        help="Provenance tag for the computation batch (default: current git short SHA).",
    )
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
    except Exception:  # pragma: no cover - defensive fallback outside a git checkout
        return "unknown"


def _load_prices_for_range(
    engine: Engine, start: date, end: date, adv_window: int
) -> pd.DataFrame:
    """Read daily_prices, extended backward by enough trailing sessions for
    the ADV window to be computable at `start` (fail-open on lookback: if
    fewer sessions exist, ADV rows simply are not emitted for the earliest
    dates in range -- see compute_price_eligibility_rows)."""
    all_dates = pd.read_sql(
        text("SELECT DISTINCT date FROM daily_prices WHERE date <= :end ORDER BY date"),
        engine,
        params={"end": end},
    )["date"].tolist()
    all_dates = sorted(pd.to_datetime(d).date() for d in all_dates)
    prior_dates = [d for d in all_dates if d < start]
    # Codex-adjacent P3 fix (03A-4b PR #42 review): the previous condition's
    # `adv_window > 1` guard routed adv_window<=1 into the "else" branch,
    # which -- rather than the intended "no lookback needed at all" -- fell
    # through to `prior_dates[0]`, fetching the ENTIRE prior history instead
    # of starting exactly at `start`. Handle adv_window<=1 explicitly (a
    # window of 1 needs zero trailing sessions).
    if adv_window <= 1:
        lookback_start = start
    elif len(prior_dates) >= adv_window - 1:
        lookback_start = prior_dates[-(adv_window - 1)]
    elif prior_dates:
        lookback_start = prior_dates[0]
    else:
        lookback_start = start
    query = text(
        "SELECT ticker, date, close, volume FROM daily_prices "
        "WHERE date >= :lookback_start AND date <= :end ORDER BY ticker, date"
    )
    return pd.read_sql(query, engine, params={"lookback_start": lookback_start, "end": end})


def run(
    universe_id: str,
    start: date,
    end: date,
    adv_window: int,
    dry_run: bool,
    code_version: Optional[str] = None,
    notes: Optional[str] = None,
    engine: Optional[Engine] = None,  # injectable for testing
    prices: Optional[pd.DataFrame] = None,  # injectable for testing
) -> None:
    from data.universe.eligibility_batch import (
        EmptyBatchError,
        compute_price_eligibility_rows,
        write_price_eligibility_batch,
    )

    if engine is None:
        import os

        engine = create_engine(os.environ["DATABASE_URL"])

    if prices is None:
        prices = _load_prices_for_range(engine, start, end, adv_window)

    if dry_run:
        # Codex-review-adjacent P2 fix (03A-4b PR #42 review): a dry-run
        # used to report "[DRY RUN] Would write 0 rows" as if that were a
        # normal preview outcome, even though the equivalent live run would
        # raise EmptyBatchError for the exact same input. A preview must
        # surface the same fail-closed condition a live run would hit, not a
        # falsely reassuring "0 rows, nothing to worry about."
        rows = compute_price_eligibility_rows(prices, start=start, end=end, adv_window=adv_window)
        if not rows:
            raise EmptyBatchError(
                f"[DRY RUN] compute_price_eligibility_rows produced zero rows for "
                f"universe_id={universe_id!r}, start={start}, end={end}. A live run "
                "with this input would raise the same error rather than write an "
                "empty batch. Check that `prices` actually covers this range and "
                f"includes at least {adv_window - 1} trailing sessions before `start`."
            )
        n_price = sum(1 for r in rows if r["attribute_name"] == "price_usd")
        n_adv = sum(1 for r in rows if r["attribute_name"] == "adv_usd_20d")
        n_tickers = len({r["ticker"] for r in rows})
        print(
            f"[DRY RUN] Would write {len(rows):,} eligibility-attribute rows "
            f"({n_price:,} price_usd, {n_adv:,} adv_usd_20d) across {n_tickers:,} "
            f"tickers for universe_id={universe_id!r}, {start} to {end}."
        )
        return

    result = write_price_eligibility_batch(
        engine,
        universe_id,
        prices,
        start=start,
        end=end,
        code_version=code_version or _default_code_version(),
        adv_window=adv_window,
        notes=notes,
    )
    logger.info(
        "eligibility_batch_complete",
        universe_id=universe_id,
        batch_id=result.batch_id,
        n_rows_written=result.n_rows_written,
        n_tickers=result.n_tickers,
        n_dates=result.n_dates,
    )
    print(
        f"Wrote batch_id={result.batch_id} with {result.n_rows_written:,} rows "
        f"across {result.n_tickers:,} tickers and {result.n_dates:,} dates "
        f"(attributes: {', '.join(result.attribute_names)})."
    )


def main() -> None:
    args = _parse_args()
    run(
        universe_id=args.universe_id,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        adv_window=args.adv_window,
        dry_run=args.dry_run,
        code_version=args.code_version,
        notes=args.notes,
    )


if __name__ == "__main__":
    main()

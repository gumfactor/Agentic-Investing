"""PIT eligibility-attribute coverage report (Roadmap 03A-4b, Phase B of
BUG-078, design doc §5.2's 03A-4 acceptance evidence).

Mirrors data/universe/import_pipeline.py::coverage_report's membership-axis
precedent (01B-2) for the eligibility axis: per-date/per-attribute row
counts, plus security_type curated-vs-default ticker counts and the
explicitly out-of-scope market_cap_usd attribute.

Usage
-----
    python -m scripts.eligibility_coverage_report \\
        --universe-id sp500 --start 2022-07-11 --end 2024-12-31 \\
        [--sample-every-n-days 21] [--output report.json]

Environment variables required: DATABASE_URL.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

load_dotenv()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PIT eligibility-attribute coverage report.")
    p.add_argument("--universe-id", default="sp500")
    p.add_argument("--start", required=True, help="First date to check (YYYY-MM-DD).")
    p.add_argument("--end", required=True, help="Last date to check (YYYY-MM-DD).")
    p.add_argument(
        "--sample-every-n-days",
        type=int,
        default=1,
        help="Check every Nth calendar day in [start, end] rather than every day "
        "(default 1 = every day; use a larger value for a fast summary over a "
        "long range).",
    )
    p.add_argument("--output", default=None, help="Optional path to write the JSON report.")
    return p.parse_args()


def _dates_in_range(start: date, end: date, step_days: int) -> list[date]:
    dates = []
    d = start
    while d <= end:
        dates.append(d)
        d += timedelta(days=step_days)
    return dates


def run(
    universe_id: str,
    start: date,
    end: date,
    sample_every_n_days: int = 1,
    output: Optional[str] = None,
    engine: Optional[Engine] = None,  # injectable for testing
) -> dict:
    from data.universe.eligibility_batch import eligibility_coverage_report

    if engine is None:
        import os

        engine = create_engine(os.environ["DATABASE_URL"])

    dates = _dates_in_range(start, end, sample_every_n_days)
    report = eligibility_coverage_report(engine, universe_id, dates)

    by_date_records = report.by_date.to_dict(orient="records") if not report.by_date.empty else []
    for rec in by_date_records:
        rec["date"] = str(rec["date"])

    # Codex P2 fix (03A-4b PR #42 review): gate on `in_coverage` directly
    # rather than re-deriving "was this row in scope" from `n_missing`.
    # `DataFrame.to_dict()` upcasts a column mixing Python `None` (the
    # out-of-coverage sentinel) with ints to float64, turning `None` into
    # `NaN` -- and `NaN not in (None, 0)` is True, so the old check counted
    # every out-of-coverage row as a gap instead of excluding it as designed.
    n_gap_rows = sum(
        1
        for r in by_date_records
        if r.get("in_coverage") and r.get("n_missing") not in (None, 0)
    )
    summary = {
        "universe_id": universe_id,
        "start": str(start),
        "end": str(end),
        "n_dates_checked": len(dates),
        "n_rows_with_gaps": n_gap_rows,
        "excluded_attributes": report.excluded_attributes,
        "n_security_type_curated_tickers": report.n_security_type_curated_tickers,
        "n_security_type_default_tickers": report.n_security_type_default_tickers,
        "by_date": by_date_records,
    }

    print(
        f"Coverage report for universe_id={universe_id!r}, {start} to {end} "
        f"({len(dates)} dates checked): {n_gap_rows} (date, attribute) rows "
        f"with a coverage gap. security_type: {report.n_security_type_curated_tickers} "
        f"hand-curated tickers, {report.n_security_type_default_tickers} default-classified. "
        f"Excluded attributes: {list(report.excluded_attributes)}."
    )
    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"Wrote report to {output}")
    return summary


def main() -> None:
    args = _parse_args()
    run(
        universe_id=args.universe_id,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        sample_every_n_days=args.sample_every_n_days,
        output=args.output,
    )


if __name__ == "__main__":
    main()

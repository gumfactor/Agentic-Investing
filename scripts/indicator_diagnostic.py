"""Run reliability and validity diagnostics on factor scores.

Loads factor_scores for a given strategy from the database and runs the
IndicatorDiagnostic checks.  Results are printed to stdout.  An optional
--output flag writes a JSON summary for downstream auditing.

Usage::

    python -m scripts.indicator_diagnostic --strategy-id v1_base_momentum

    python -m scripts.indicator_diagnostic \\
        --strategy-id v1_base_momentum \\
        --start-date 2023-01-01 --end-date 2024-12-31

    python -m scripts.indicator_diagnostic \\
        --strategy-id v1_base_momentum \\
        --output local/indicator_diagnostic.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.validation.indicator_diagnostic import (
    DiagnosticReport,
    IndicatorDiagnostic,
    format_report,
)


# ─── DB loading ───────────────────────────────────────────────────────────────

# BUG-072 (adversarial review round 8, closing the last open item): factor_scores'
# PK/unique constraints were widened by migration 012 to include
# research_run_id, so more than one row can legitimately exist for the same
# (ticker, score_date, strategy_id, factor_name) across research runs
# (legacy, superseded, active). Unlike the dashboard staleness cases fixed
# earlier in BUG-072, this is a real correctness risk for THIS tool's own
# stated purpose: _load_factor_scores previously loaded by strategy_id/date
# range only, so a mixed-run blend could silently reach the diagnostic
# pivot, which averages duplicate (ticker, score_date, factor_name) rows
# instead of failing (see the `indicator_diagnostic_duplicate_rows` warning
# in backtesting/validation/indicator_diagnostic.py) -- producing a
# reliability/validity report computed across methodologies without any
# indication that happened. Default behavior now matches every other reader
# fixed under BUG-072: filter to the single active
# daily_signal_pipeline_operational run. `--all-runs` is a documented,
# explicit opt-in escape hatch for genuine cross-run diagnostic comparisons
# (the design plan's "explicit opt-in for cross-run reads" principle) --
# using it still fails closed if the resulting blend contains duplicate
# (ticker, score_date, factor_name) rows, rather than silently averaging.
_ACTIVE_RUN_SUBQUERY = """(
    SELECT rr.id FROM research_runs rr
    JOIN research_methodologies rm ON rm.id = rr.methodology_id
    WHERE rm.name = 'daily_signal_pipeline_operational' AND rr.is_active = TRUE
)"""


def _load_factor_scores(
    engine,
    strategy_id: str,
    start_date: date | None,
    end_date: date | None,
    all_runs: bool = False,
) -> pd.DataFrame:
    clauses = ["strategy_id = :strategy_id"]
    params: dict = {"strategy_id": strategy_id}

    if start_date is not None:
        clauses.append("score_date >= :start_date")
        params["start_date"] = start_date
    if end_date is not None:
        clauses.append("score_date <= :end_date")
        params["end_date"] = end_date
    if not all_runs:
        clauses.append(f"research_run_id = {_ACTIVE_RUN_SUBQUERY}")

    where = " AND ".join(clauses)
    sql = text(
        f"SELECT ticker, score_date, factor_name, z_score "
        f"FROM factor_scores "
        f"WHERE {where} "
        f"ORDER BY score_date, ticker, factor_name"
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params=params)

    if all_runs:
        n_dupes = df.duplicated(subset=["ticker", "score_date", "factor_name"]).sum()
        if n_dupes:
            raise ValueError(
                f"--all-runs produced {n_dupes} duplicate (ticker, score_date, "
                "factor_name) rows across research runs. Refusing to silently "
                "blend/average them into the diagnostic (BUG-072). Re-run "
                "without --all-runs to scope to the single active run, or "
                "narrow --start-date/--end-date/--strategy-id to a window "
                "that resolves to one run."
            )

    return df


# ─── JSON serialisation ───────────────────────────────────────────────────────

def _report_to_dict(report: DiagnosticReport) -> dict:
    def _f(v: float) -> float | None:
        return None if math.isnan(v) else v

    return {
        "schema_version": 1,
        "strategy_id": report.strategy_id,
        "n_factors": report.n_factors,
        "n_dates": report.n_dates,
        "n_tickers": report.n_tickers,
        "date_range": [str(report.date_range[0]), str(report.date_range[1])],
        "summary": report.summary,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reliability": [
            {
                "factor_name": r.factor_name,
                "category": r.category,
                "n_observations": r.n_observations,
                "nan_rate": _f(r.nan_rate),
                "mean_bias": _f(r.mean_bias),
                "std_mean": _f(r.std_mean),
                "std_stability": _f(r.std_stability),
                "outlier_rate": _f(r.outlier_rate),
                "median_rank_autocorr": _f(r.median_rank_autocorr),
                "flags": r.flags,
                "reliable": r.reliable,
            }
            for r in report.reliability
        ],
        "validity": {
            "within_category_mean_abs_corr": _f(report.validity.within_category_mean),
            "cross_category_mean_abs_corr": _f(report.validity.cross_category_mean),
            "high_correlation_pairs": [
                {"factor_a": a, "factor_b": b, "correlation": _f(r)}
                for a, b, r in report.validity.high_correlation_pairs
            ],
            "low_within_category_pairs": [
                {"factor_a": a, "factor_b": b, "correlation": _f(r)}
                for a, b, r in report.validity.low_within_category_pairs
            ],
            "flags": report.validity.flags,
        },
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Indicator reliability and validity diagnostic"
    )
    parser.add_argument(
        "--strategy-id",
        required=True,
        help="Strategy ID to diagnose (matches strategy_id in factor_scores table)",
    )
    parser.add_argument(
        "--start-date",
        help="Earliest score_date to include, YYYY-MM-DD (optional)",
    )
    parser.add_argument(
        "--end-date",
        help="Latest score_date to include, YYYY-MM-DD (optional)",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write JSON report (e.g. local/indicator_diagnostic.json)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any factor raises a reliability or validity flag (default: always exit 0)",
    )
    parser.add_argument(
        "--all-runs",
        action="store_true",
        help=(
            "Explicit opt-in to load factor_scores across ALL research runs "
            "instead of only the active daily_signal_pipeline_operational "
            "run (BUG-072). Fails closed (raises) if the resulting blend "
            "contains duplicate (ticker, score_date, factor_name) rows "
            "rather than silently averaging them into the diagnostic."
        ),
    )
    args = parser.parse_args(argv)

    start_date = date.fromisoformat(args.start_date) if args.start_date else None
    end_date = date.fromisoformat(args.end_date) if args.end_date else None

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("FAIL: DATABASE_URL environment variable is not set")
        return 1

    engine = create_engine(database_url, pool_pre_ping=True, pool_size=1)

    print(f"INFO: loading factor_scores for strategy_id={args.strategy_id!r}", flush=True)
    try:
        factor_scores = _load_factor_scores(
            engine, args.strategy_id, start_date, end_date, all_runs=args.all_runs
        )
    except SQLAlchemyError as exc:
        print(f"FAIL: database error loading factor_scores — {exc}")
        return 1
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1

    if factor_scores.empty:
        print(
            f"FAIL: no factor_scores found for strategy_id={args.strategy_id!r}"
            + (f", start_date={start_date}" if start_date else "")
            + (f", end_date={end_date}" if end_date else "")
        )
        return 1

    n_rows = len(factor_scores)
    n_factors = factor_scores["factor_name"].nunique()
    print(f"INFO: loaded {n_rows:,} rows, {n_factors} factors", flush=True)

    diag = IndicatorDiagnostic()
    try:
        report = diag.run(factor_scores, strategy_id=args.strategy_id)
    except ValueError as exc:
        print(f"FAIL: diagnostic error — {exc}")
        return 1

    print()
    print(format_report(report))

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            print(f"\nFAIL: output path already exists: {out_path}  (remove it or choose a different path)")
            return 1
        out_path.write_text(json.dumps(_report_to_dict(report), indent=2))
        print(f"\nINFO: report written to {out_path}")

    n_reliability_warn = report.n_factors - report.n_reliable
    n_validity_flags = len(report.validity.flags)
    return 1 if (args.strict and (n_reliability_warn > 0 or n_validity_flags > 0)) else 0


if __name__ == "__main__":
    sys.exit(main())

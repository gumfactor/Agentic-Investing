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

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.validation.indicator_diagnostic import (
    DiagnosticReport,
    IndicatorDiagnostic,
    format_report,
)


# ─── DB loading ───────────────────────────────────────────────────────────────

def _load_factor_scores(
    engine,
    strategy_id: str,
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame:
    clauses = ["strategy_id = :strategy_id"]
    params: dict = {"strategy_id": strategy_id}

    if start_date is not None:
        clauses.append("score_date >= :start_date")
        params["start_date"] = start_date
    if end_date is not None:
        clauses.append("score_date <= :end_date")
        params["end_date"] = end_date

    where = " AND ".join(clauses)
    sql = text(
        f"SELECT ticker, score_date, factor_name, z_score "
        f"FROM factor_scores "
        f"WHERE {where} "
        f"ORDER BY score_date, ticker, factor_name"
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params=params)

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
        factor_scores = _load_factor_scores(engine, args.strategy_id, start_date, end_date)
    except Exception as exc:
        print(f"FAIL: could not load factor_scores — {exc}")
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

    n_warn = report.n_factors - report.n_reliable
    return 1 if n_warn > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

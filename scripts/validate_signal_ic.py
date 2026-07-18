"""Validate Phase 2 factor IC on live TimescaleDB data.

The command recomputes historical factor scores from ``daily_prices``, reserves
the final portion of trading dates as a chronological holdout, and evaluates
21- and 63-trading-day forward returns. Results can be persisted to
``signal_ic_stats`` for traceability.

Usage:
    python scripts/validate_signal_ic.py
    python scripts/validate_signal_ic.py --factors momentum lowvol --persist

FROZEN HOLDOUT WARNING
----------------------
The Phase 2 holdout boundary (final 30% of dates as of 2026-06-09) was
evaluated and its results recorded in the Worklog and PRD on 2026-06-10.
That boundary is now **frozen**.

Do NOT use this script to iteratively improve a factor and retest on the same
holdout.  Every such rerun leaks information — the researcher knows the boundary
date, which creates implicit look-ahead bias even without touching holdout data
directly.

For any new pre-specified factor or methodology change introduced AFTER
2026-06-10, use one of:
  - A later out-of-sample window (extend the dataset, reserve the newest dates)
  - A fully walk-forward design that never back-calculates using the known split
  - A separate held-out segment explicitly set aside before development begins

The existing momentum result is valid because it was evaluated before the
holdout results were used to guide any implementation decision.  Subsequent
factors must follow the same discipline.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from signals.composites.low_vol_score import compute_lowvol_scores
from signals.composites.momentum_score import compute_momentum_scores
from signals.composites.quality_score import compute_quality_scores
from signals.composites.value_score import compute_value_scores
from signals.research.ic import (
    compute_factor_turnover,
    compute_ic_series,
    rolling_ic_summary,
    summarize_ic,
)
from signals.research.universe import audit_universe_survivorship

load_dotenv()

_DEFAULT_HORIZONS = [21, 63]
_DEFAULT_STRATEGY_ID = "v1_base_momentum"


@dataclass(frozen=True)
class FactorSpec:
    compute: Callable
    score_col: str
    needs_fundamentals: bool = False


_FACTORS = {
    "momentum": FactorSpec(compute_momentum_scores, "momentum_score"),
    "lowvol": FactorSpec(compute_lowvol_scores, "lowvol_score"),
    "value": FactorSpec(
        compute_value_scores,
        "value_score",
        needs_fundamentals=True,
    ),
    "quality": FactorSpec(
        compute_quality_scores,
        "quality_score",
        needs_fundamentals=True,
    ),
}


def _holdout_start(dates: list, train_fraction: float) -> object:
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be between 0 and 1")
    if len(dates) < 2:
        raise ValueError("at least two trading dates are required")
    split_index = min(int(len(dates) * train_fraction), len(dates) - 1)
    return dates[split_index]


def _add_gate_columns(
    summary: pd.DataFrame,
    min_ic: float,
    min_tstat: float,
) -> pd.DataFrame:
    out = summary.copy()
    out["passes_ic"] = out["ic"] >= min_ic
    out["passes_tstat"] = out["ic_tstat"] >= min_tstat
    out["passes_gate"] = out["passes_ic"] & out["passes_tstat"]
    return out


def _build_eligibility_frame(universe_lookup, dates: list) -> pd.DataFrame:
    """Long-format (ticker, date) PIT eligibility frame for factor scoring.

    Built from the same PITUniverseLookup used for IC merging, so the
    scoring cross-section and the IC membership filter are guaranteed to
    agree. Fails closed (CoverageGapError propagates) when any requested
    date is outside validated coverage.
    """
    rows: list[dict] = []
    for d in dates:
        eligible = universe_lookup.load_universe_as_of(d).eligible_tickers
        rows.extend({"ticker": t, "date": d} for t in eligible)
    return pd.DataFrame(rows, columns=["ticker", "date"])


def _persist_summary(engine, summary: pd.DataFrame, provisional: bool = True) -> int:
    """Persist IC summary rows.

    ``provisional`` stamps each row (migration 010): True for runs without
    PIT universe enforcement (--provisional-no-universe and all pre-01B-2
    rows), False for PIT-enforced runs. Interim marker — superseded by the
    01B-3 research-run identity (design plan §4).
    """
    if summary.empty:
        return 0

    records = summary[
        [
            "factor_name",
            "strategy_id",
            "eval_date",
            "horizon_days",
            "ic",
            "rank_ic",
            "ic_tstat",
            "ic_ir",
            "ic_pvalue",
            "n_observations",
        ]
    ].to_dict("records")
    for record in records:
        record["provisional"] = provisional

    statement = text(
        "INSERT INTO signal_ic_stats "
        "(factor_name, strategy_id, eval_date, horizon_days, ic, rank_ic, "
        "ic_tstat, ic_ir, ic_pvalue, n_observations, provisional) "
        "VALUES (:factor_name, :strategy_id, :eval_date, :horizon_days, :ic, "
        ":rank_ic, :ic_tstat, :ic_ir, :ic_pvalue, :n_observations, :provisional) "
        "ON CONFLICT (factor_name, strategy_id, eval_date, horizon_days) "
        "DO UPDATE SET ic = EXCLUDED.ic, rank_ic = EXCLUDED.rank_ic, "
        "ic_tstat = EXCLUDED.ic_tstat, ic_ir = EXCLUDED.ic_ir, "
        "ic_pvalue = EXCLUDED.ic_pvalue, "
        "n_observations = EXCLUDED.n_observations, "
        "provisional = EXCLUDED.provisional, computed_at = NOW()"
    )
    with engine.begin() as connection:
        connection.execute(statement, records)
    return len(records)


def _load_prices(engine) -> pd.DataFrame:
    prices = pd.read_sql(
        "SELECT ticker, date, close FROM daily_prices ORDER BY date, ticker",
        engine,
    )
    prices["date"] = pd.to_datetime(prices["date"]).dt.date
    return prices


def _load_fundamentals(engine) -> pd.DataFrame:
    fundamentals = pd.read_sql(
        "SELECT ticker, period_end_date, release_date, period_type, "
        "item_name, value FROM financial_statements "
        "ORDER BY release_date, period_end_date",
        engine,
    )
    for column in ["period_end_date", "release_date"]:
        fundamentals[column] = pd.to_datetime(fundamentals[column]).dt.date
    fundamentals["value"] = fundamentals["value"].astype(float)
    return fundamentals


def _print_summary(
    factor_name: str,
    summary: pd.DataFrame,
    rolling: pd.DataFrame,
    turnover: pd.DataFrame,
) -> None:
    print(f"\nFactor: {factor_name}")
    display_cols = [
        "horizon_days",
        "ic",
        "rank_ic",
        "ic_ir",
        "ic_tstat",
        "ic_pvalue",
        "n_observations",
        "passes_gate",
    ]
    print(summary[display_cols].to_string(index=False))

    if not rolling.empty:
        latest = (
            rolling.sort_values(["horizon_days", "score_date"])
            .groupby("horizon_days")
            .tail(1)
        )
        print("\nLatest rolling IC:")
        print(
            latest[
                [
                    "score_date",
                    "horizon_days",
                    "ic_mean",
                    "rank_ic_mean",
                    "ic_ir",
                    "hit_rate",
                    "n_dates",
                ]
            ].to_string(index=False)
        )

    if not turnover.empty:
        print(
            "\n21-day rank autocorrelation: "
            f"{turnover['rank_autocorrelation'].mean():.4f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--factors",
        nargs="+",
        choices=sorted(_FACTORS),
        default=["momentum", "lowvol", "value", "quality"],
    )
    parser.add_argument("--horizons", nargs="+", type=int, default=_DEFAULT_HORIZONS)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--strategy-id", default=_DEFAULT_STRATEGY_ID)
    parser.add_argument("--min-ic", type=float, default=0.03)
    parser.add_argument("--min-tstat", type=float, default=2.0)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument(
        "--universe-id",
        default="sp500",
        help="Point-in-time universe for membership enforcement (default: sp500).",
    )
    parser.add_argument(
        "--provisional-no-universe",
        action="store_true",
        help="Skip point-in-time membership enforcement (BUG-008). Results are "
        "PROVISIONAL: not valid for selection, promotion, or paper-trading "
        "qualification.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 1

    engine = create_engine(database_url)
    prices = _load_prices(engine)
    dates = sorted(prices["date"].unique())
    holdout_start = _holdout_start(dates, args.train_fraction)
    audit = audit_universe_survivorship(prices)
    fundamentals = None

    # ── Point-in-time universe (BUG-008 / 01B-2) ─────────────────────────────
    # This is a HISTORICAL caller: membership enforcement is required by
    # default and fails closed when no published universe import exists or
    # when any holdout date is outside validated coverage.
    universe_lookup = None
    if args.provisional_no_universe:
        print(
            "WARNING: --provisional-no-universe set. IC results are PROVISIONAL "
            "(current-membership universe, BUG-008): not valid for selection, "
            "promotion, or paper-trading qualification."
        )
    else:
        from data.universe.runtime import NoPublishedImportError, PITUniverseLookup

        try:
            universe_lookup = PITUniverseLookup(engine, args.universe_id)
        except NoPublishedImportError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            print(
                "Run scripts/import_universe_membership.py first, or rerun with "
                "--provisional-no-universe to accept provisional results.",
                file=sys.stderr,
            )
            return 1
        print(
            f"PIT universe: {args.universe_id} import batch "
            f"{universe_lookup.import_batch_id}, coverage "
            f"[{universe_lookup.coverage_start}, {universe_lookup.coverage_end}]"
        )

    print(
        f"Live prices: {len(prices):,} rows, {prices['ticker'].nunique()} tickers, "
        f"{len(dates)} dates ({dates[0]} to {dates[-1]})"
    )
    print(
        f"Holdout: {holdout_start} onward "
        f"(final {(1.0 - args.train_fraction):.0%} of trading dates)"
    )
    if universe_lookup is None:
        # Survivorship warning applies only to provisional (non-PIT) runs;
        # with membership enforcement active it would cry wolf.
        print(audit["warning"])

    # ── PIT scoring cross-section (BUG-008 / Codex PR #34 P1) ────────────────
    # Membership must define each factor's cross-section BEFORE its
    # z-scoring: passing the lookup only to compute_ic_series would leave
    # non-members contaminating the cross-sectional mean/std that member
    # scores (persisted with provisional=false) are built from.
    eligibility_df = None
    if universe_lookup is not None:
        eligibility_df = _build_eligibility_frame(universe_lookup, dates)

    summaries: list[pd.DataFrame] = []
    for factor_name in args.factors:
        spec = _FACTORS[factor_name]
        if spec.needs_fundamentals:
            if fundamentals is None:
                fundamentals = _load_fundamentals(engine)
            holdout_dates = [date for date in dates if date >= holdout_start]
            scores = spec.compute(
                fundamentals,
                prices,
                score_dates=holdout_dates,
                eligibility=eligibility_df,
            )
        else:
            scores = spec.compute(prices, eligibility=eligibility_df)
        holdout_scores = scores[scores["date"] >= holdout_start].copy()

        ic_series = compute_ic_series(
            holdout_scores,
            prices,
            score_col=spec.score_col,
            horizons=args.horizons,
            universe=universe_lookup,
        )
        summary = summarize_ic(
            ic_series,
            factor_name=factor_name,
            strategy_id=args.strategy_id,
        )
        summary = _add_gate_columns(summary, args.min_ic, args.min_tstat)
        rolling = rolling_ic_summary(ic_series, trailing_dates=252, min_dates=30)
        turnover = compute_factor_turnover(
            holdout_scores,
            score_col=spec.score_col,
            rebalance_days=21,
        )
        _print_summary(factor_name, summary, rolling, turnover)
        summaries.append(summary)

    combined = pd.concat(summaries, ignore_index=True)
    expected_tests = len(args.factors) * len(args.horizons)
    passed_tests = int(combined["passes_gate"].sum())

    if args.persist:
        persisted = _persist_summary(engine, combined, provisional=universe_lookup is None)
        print(f"\nPersisted {persisted} rows to signal_ic_stats.")

    print(
        f"\nGate result: {passed_tests}/{expected_tests} factor-horizon tests pass "
        f"(IC >= {args.min_ic:.2%}, t-stat >= {args.min_tstat:.2f})."
    )
    return 0 if passed_tests == expected_tests else 2


if __name__ == "__main__":
    sys.exit(main())

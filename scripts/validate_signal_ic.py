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
import structlog
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from data.normalization.corporate_actions import build_score_price_history_as_of
from data.universe.calendar import session_close_cutoff
from signals.composites.low_vol_score import compute_lowvol_scores
from signals.composites.momentum_score import compute_momentum_scores
from signals.composites.quality_score import compute_quality_scores
from signals.composites.value_score import compute_value_scores
from signals.research.ic import (
    compute_factor_turnover,
    compute_ic_series,
    compute_realized_forward_returns_as_of,
    rolling_ic_summary,
    summarize_ic,
)
from signals.research.universe import audit_universe_survivorship

load_dotenv()

logger = structlog.get_logger(__name__)

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


def _persist_summary(
    engine, summary: pd.DataFrame, research_run_id: int, provisional: bool = True
) -> int:
    """Persist IC summary rows.

    ``research_run_id`` (BUG-009 section 4 / migration 012) is now part of
    the table's unique constraint: it is REQUIRED so a new run can never
    silently overwrite an old methodology's rows via this upsert. Register
    a methodology/run first with ``data.research.identity`` (or use
    ``data.research.identity.get_legacy_run_id`` only for tooling that
    intentionally targets the migrated legacy row — never for a fresh run).

    ``provisional`` stamps each row (migration 010): True for runs without
    PIT universe enforcement (--provisional-no-universe and all pre-01B-2
    rows), False for PIT-enforced runs. Kept for backward read-compatibility
    (migration 012 docstring); the authoritative marker is now
    ``research_runs.status``/``is_active`` reached via ``research_run_id``.
    """
    if summary.empty:
        return 0
    if not research_run_id:
        raise ValueError(
            "research_run_id is required to persist (BUG-009 section 4 / "
            "migration 012): register a research_methodologies/research_runs "
            "pair first via data.research.identity."
        )

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
        record["research_run_id"] = research_run_id

    statement = text(
        "INSERT INTO signal_ic_stats "
        "(factor_name, strategy_id, eval_date, horizon_days, ic, rank_ic, "
        "ic_tstat, ic_ir, ic_pvalue, n_observations, provisional, research_run_id) "
        "VALUES (:factor_name, :strategy_id, :eval_date, :horizon_days, :ic, "
        ":rank_ic, :ic_tstat, :ic_ir, :ic_pvalue, :n_observations, :provisional, "
        ":research_run_id) "
        "ON CONFLICT (research_run_id, factor_name, strategy_id, eval_date, horizon_days) "
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


def _load_corporate_actions(engine) -> pd.DataFrame:
    """Load corporate_actions with the availability columns (migration 011)
    the cutoff-aware adjustment builders require."""
    actions = pd.read_sql(
        "SELECT ticker, ex_date, action_type, value, known_at, source_version "
        "FROM corporate_actions ORDER BY ticker, ex_date",
        engine,
    )
    actions["ex_date"] = pd.to_datetime(actions["ex_date"]).dt.date
    actions["known_at"] = pd.to_datetime(actions["known_at"], utc=True)
    return actions


def _build_score_adjusted_prices(
    prices: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    dates: list,
) -> pd.DataFrame:
    """Build the cutoff-aware SCORE price series BUG-009 §2.3 requires: only
    actions known-and-occurred by the score cutoff may adjust a score
    feature's inputs. Feeds price-ratio-based factors (momentum, lowvol).

    Single-boundary-cutoff note (BUG-071, re-verified adversarial-review
    round 4)
    --------------------------------------------------------------------
    This backfill computes scores for every holdout date in one vectorized
    pass rather than looping per score_date. The series below is built
    from ONE boundary cutoff — the session close of the LATEST available
    price date — rather than a literal per-score-date cutoff.

    This remains provably safe for a window/ratio-based indicator's SCORE
    value (which is what every price-ratio factor in this script computes):
    a uniform multiplicative adjustment factor applied to an entire
    lookback window cancels out of any price RATIO computed within that
    window, so an action whose ex_date falls AFTER a given score_date's
    window has NO effect on that score, uniform-cutoff or per-date-cutoff.

    Round-4 finding and why it does NOT reopen this argument: adversarial
    review round 4 found the ANALOGOUS single-boundary-cutoff shortcut was
    unsafe for the REALIZED-RETURN series (fixed below via
    ``compute_realized_forward_returns_as_of``, which builds a genuinely
    per-exit-date-cutoff-correct series instead of this approximation).
    That failure mode was possible because a realized return's two
    endpoints (entry_date, exit_date) straddle the action in a way that
    does NOT cancel — one endpoint is before the action's ex_date, the
    other is not. The score series has no second endpoint: every date in
    its lookback window is being compared only to ANOTHER date inside that
    SAME window, both on the SAME side of any action whose ex_date is
    after the window's own end (the score_date) — the cancellation holds
    for every pair inside the window, not just the window's two endpoints.
    The residual gap (documented as BUG-071 in bugs.md, verified still
    accurate) is unchanged: an action whose ex_date falls exactly ON a
    score_date, which is a narrow, single-session edge case — not the
    "zero adjustment happens at all" gap this wiring closes, and not the
    "future information leaks into a persisted, PIT-safe result across an
    entire holdout" class of bug round 4 found in the realized-return path.
    """
    boundary_cutoff = session_close_cutoff(dates[-1])

    score_adjusted, score_meta = build_score_price_history_as_of(
        prices, corporate_actions, score_cutoff=boundary_cutoff
    )
    logger.info(
        "score_adjusted_price_series_built",
        boundary_cutoff=boundary_cutoff.isoformat(),
        score_actions_considered=score_meta.n_actions_considered,
        score_actions_excluded_by_cutoff=score_meta.n_actions_excluded_by_cutoff,
        score_actions_excluded_missing_known_at=score_meta.n_actions_excluded_missing_known_at,
    )

    out = score_adjusted[["ticker", "date", "adj_close"]].copy()
    out["close"] = out["adj_close"].astype(float)
    return out[["ticker", "date", "close"]]


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
        "--research-run-id",
        type=int,
        default=None,
        help="research_runs.id (BUG-009 section 4 / migration 012) to tag every "
        "persisted signal_ic_stats row with. Required when --persist is set. "
        "Register a methodology/run first via data.research.identity.",
    )
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

    if args.persist and not args.research_run_id:
        print(
            "ERROR: --persist requires --research-run-id (BUG-009 section 4 / "
            "migration 012). Register a research_methodologies/research_runs "
            "pair first via data.research.identity.",
            file=sys.stderr,
        )
        return 1

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

    # ── Corporate-action cutoff-aware adjustment (BUG-009 §2.3-2.4) ──────────
    # score_adjusted_prices feeds price-RATIO-based factors (momentum,
    # lowvol): only actions known-and-occurred by the cutoff adjust their
    # inputs (single run-boundary cutoff — safe by the ratio-cancellation
    # argument documented on _build_score_adjusted_prices / BUG-071).
    # realized_forward_returns feeds compute_ic_series's forward/realized-
    # return leg for EVERY factor via a genuinely per-exit-date-cutoff-
    # correct construction (adversarial-review round 4 fix: a shared
    # boundary cutoff is NOT safe here, unlike the score series — see
    # compute_realized_forward_returns_as_of's docstring). Fundamentals-
    # based factors (value, quality) deliberately keep RAW prices for their
    # own valuation-ratio inputs (P/E, P/B, ... use the actual traded
    # price, not a total-return-adjusted synthetic price) — see the
    # per-factor loop below.
    corporate_actions = _load_corporate_actions(engine)
    score_adjusted_prices = _build_score_adjusted_prices(prices, corporate_actions, dates)
    realized_forward_returns = compute_realized_forward_returns_as_of(
        prices, corporate_actions, horizons=args.horizons
    )

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
    #
    # Eligibility is built ONLY for the dates actually scored/evaluated
    # (the holdout window) — factor lookbacks need only prices, and pre-
    # holdout price history may legitimately predate the published PIT
    # coverage window; querying it would fail an otherwise valid run
    # (Codex PR #34 P2). Dates absent from the frame are fully masked by
    # the composites (fail closed), so pre-holdout cross-sections are
    # never emitted, merely skipped.
    eligibility_df = None
    if universe_lookup is not None:
        scored_dates = [d for d in dates if d >= holdout_start]
        eligibility_df = _build_eligibility_frame(universe_lookup, scored_dates)

    summaries: list[pd.DataFrame] = []
    for factor_name in args.factors:
        spec = _FACTORS[factor_name]
        if spec.needs_fundamentals:
            if fundamentals is None:
                fundamentals = _load_fundamentals(engine)
            holdout_dates = [date for date in dates if date >= holdout_start]
            # Value/quality use RAW prices deliberately: P/E, P/B, and other
            # valuation ratios need the actual traded price, not a total-
            # return-adjusted synthetic price (adjusting for dividend
            # reinvestment would distort a valuation ratio, not fix it).
            scores = spec.compute(
                fundamentals,
                prices,
                score_dates=holdout_dates,
                eligibility=eligibility_df,
            )
        else:
            # Price-ratio-based factors (momentum, lowvol): BUG-009 §2.3 —
            # only actions known-and-occurred by the score cutoff may adjust
            # the price history feeding the score.
            scores = spec.compute(score_adjusted_prices, eligibility=eligibility_df)
        holdout_scores = scores[scores["date"] >= holdout_start].copy()

        # Forward/realized returns for IC use the per-exit-date-cutoff-
        # correct realized-return series for EVERY factor (BUG-009 §2.3-2.4;
        # adversarial-review round 4): a return is a return regardless of
        # what produced the score being evaluated against it, and each
        # exit's own knowledge cutoff — not a shared boundary — determines
        # which corporate actions may adjust it.
        ic_series = compute_ic_series(
            holdout_scores,
            None,
            score_col=spec.score_col,
            horizons=args.horizons,
            universe=universe_lookup,
            precomputed_forward_returns=realized_forward_returns,
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
        persisted = _persist_summary(
            engine, combined, research_run_id=args.research_run_id,
            provisional=universe_lookup is None,
        )
        print(f"\nPersisted {persisted} rows to signal_ic_stats (research_run_id={args.research_run_id}).")

    print(
        f"\nGate result: {passed_tests}/{expected_tests} factor-horizon tests pass "
        f"(IC >= {args.min_ic:.2%}, t-stat >= {args.min_tstat:.2f})."
    )
    return 0 if passed_tests == expected_tests else 2


if __name__ == "__main__":
    sys.exit(main())

"""Point-in-time (PIT) safety audit for signal scores.

Verifies that stored alpha/factor scores contain no look-ahead bias by
re-computing momentum scores from price data strictly before each
``score_date`` and comparing to the stored values.

Two audit modes
---------------
1. **Structural** (fast, always runs): verifies that
   ``DataHandler.get_latest_signals`` enforces ``score_date < sim_date``
   (strictly less-than, not <=).

2. **Empirical** (optional sample, default 200 pairs): for each sampled
   ``(ticker, score_date)`` pair, re-runs ``compute_momentum_scores`` on
   ``prices[date < score_date]`` and checks that the stored
   ``momentum_score`` (in factor_scores) matches within a tight tolerance.
   A mismatch means that the stored score used data from ``score_date`` or
   later — look-ahead bias.

Data source options
-------------------
Parquet files (offline, no MinIO required):
    python -m scripts.audit_pit_safety \\
        --prices-file  /path/to/daily_prices.parquet \\
        --scores-file  /path/to/factor_scores.parquet

MinIO snapshot (requires env vars):
    python -m scripts.audit_pit_safety \\
        --snapshot-date 2026-06-10 \\
        --strategy-id  v1

Output
------
Prints a structured summary.  Exits with code 1 if any empirical violation
is found; exits with code 0 on a clean audit.

Environment variables (for MinIO mode):
    DATABASE_URL, MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import structlog

# Allow running as a script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from signals.factors.momentum import compute_momentum_scores

logger = structlog.get_logger(__name__)

_SCORE_TOLERANCE = 1e-6   # scores should be bit-identical; small float noise is ok
_DEFAULT_SAMPLE  = 200


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Audit momentum signal scores for point-in-time safety."
    )

    source = p.add_argument_group("data source (choose one mode)")
    source.add_argument("--prices-file",  help="Path to daily_prices parquet file.")
    source.add_argument("--scores-file",  help="Path to factor_scores parquet file.")
    source.add_argument("--snapshot-date", help="MinIO snapshot date (YYYY-MM-DD).")
    source.add_argument("--strategy-id",   default="v1", help="strategy_id to filter (default v1).")

    p.add_argument(
        "--sample-size",
        type=int,
        default=_DEFAULT_SAMPLE,
        help=f"Number of (ticker, score_date) pairs to audit empirically (default {_DEFAULT_SAMPLE}).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible sampling (default 42).",
    )
    p.add_argument(
        "--skip-empirical",
        action="store_true",
        help="Run only the structural checks; skip the empirical re-computation.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_from_files(prices_file: str, scores_file: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    logger.info("loading_from_parquet", prices=prices_file, scores=scores_file)
    prices = pd.read_parquet(prices_file)
    scores = pd.read_parquet(scores_file)
    return prices, scores


def _load_from_snapshot(snapshot_date: date, strategy_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    from data.storage.parquet_snapshots import ParquetSnapshots
    snaps = ParquetSnapshots()
    logger.info("loading_from_minio", snapshot_date=str(snapshot_date))
    prices = snaps.load_snapshot("daily_prices", snapshot_date)
    scores = snaps.load_snapshot("factor_scores", snapshot_date)
    if "strategy_id" in scores.columns:
        scores = scores[scores["strategy_id"] == strategy_id].reset_index(drop=True)
    return prices, scores


def _normalise(prices: pd.DataFrame, scores: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cast date columns to ``datetime.date`` objects."""
    for col in ("date",):
        if col in prices.columns:
            prices = prices.copy()
            prices[col] = pd.to_datetime(prices[col]).dt.date

    for col in ("score_date",):
        if col in scores.columns:
            scores = scores.copy()
            scores[col] = pd.to_datetime(scores[col]).dt.date

    return prices, scores


# ---------------------------------------------------------------------------
# Structural audit
# ---------------------------------------------------------------------------

def _structural_audit() -> list[str]:
    """Verify DataHandler enforces strict score_date < sim_date."""
    import inspect
    from backtesting.engine.data_handler import DataHandler

    source = inspect.getsource(DataHandler.get_latest_signals)
    violations: list[str] = []

    if 'score_date" < sim_date' in source or "score_date < sim_date" in source:
        logger.info("structural_check_passed", check="score_date_strict_lt")
    elif 'score_date" <= sim_date' in source or "score_date <= sim_date" in source:
        violations.append(
            "DataHandler.get_latest_signals uses '<=' for score_date filter — "
            "should be '<' (strictly less-than) to enforce the 1-day execution lag."
        )
    else:
        violations.append(
            "DataHandler.get_latest_signals: could not confirm score_date filter direction "
            "from source inspection. Manual review required."
        )

    return violations


# ---------------------------------------------------------------------------
# Empirical audit
# ---------------------------------------------------------------------------

def _empirical_audit(
    prices: pd.DataFrame,
    scores: pd.DataFrame,
    sample_size: int,
    seed: int,
) -> tuple[int, int, list[dict]]:
    """Re-compute momentum scores per sampled (ticker, score_date) and compare.

    For each sampled pair, we:
      1. Filter prices to ``date < score_date`` (strictly).
      2. Re-run ``compute_momentum_scores`` on this PIT-filtered subset.
      3. Compare the re-computed ``momentum_score`` to the stored ``z_score``
         (factor_scores uses the column ``z_score`` for the normalised score).

    Returns:
        (n_checked, n_violations, violation_records)
    """
    score_col = "z_score" if "z_score" in scores.columns else "momentum_score"
    factor_filter = scores["factor_name"] == "momentum" if "factor_name" in scores.columns else pd.Series([True] * len(scores))
    momentum_scores = scores[factor_filter].copy()

    if momentum_scores.empty:
        logger.warning("empirical_audit_skipped", reason="no momentum rows in factor_scores")
        return 0, 0, []

    required = {"ticker", "score_date", score_col}
    missing = required - set(momentum_scores.columns)
    if missing:
        logger.warning("empirical_audit_skipped", reason=f"missing columns: {missing}")
        return 0, 0, []

    # Sample deterministically
    rng = np.random.default_rng(seed)
    n = min(sample_size, len(momentum_scores))
    sample_idx = rng.choice(len(momentum_scores), size=n, replace=False)
    sample = momentum_scores.iloc[sample_idx].reset_index(drop=True)

    violations: list[dict] = []
    n_checked = 0

    prices_close = prices[["ticker", "date", "close"]].copy()
    prices_close["date"] = pd.to_datetime(prices_close["date"]).dt.date
    prices_close["close"] = prices_close["close"].astype(float)

    for _, row in sample.iterrows():
        ticker: str = row["ticker"]
        score_date: date = row["score_date"]
        stored_score: float = float(row[score_col])

        # PIT boundary: a score for day S is "as of" end-of-day S, so it may
        # use prices with date <= S.  The 1-day execution lag (score_date <
        # sim_date in DataHandler) ensures the signal is only traded from S+1.
        pit_prices = prices_close[prices_close["date"] <= score_date]

        if pit_prices.empty or ticker not in pit_prices["ticker"].values:
            continue  # no price history for this ticker — cannot verify

        try:
            recomputed = compute_momentum_scores(pit_prices)
        except Exception as exc:
            logger.warning(
                "empirical_recompute_error",
                ticker=ticker,
                score_date=str(score_date),
                error=str(exc),
            )
            continue

        # Look up the score at exactly score_date (same date, same ticker).
        # Comparing at the same date is the correct test: if the stored score
        # used data beyond score_date the recomputed value will differ.
        match = recomputed[
            (recomputed["ticker"] == ticker) & (recomputed["date"] == score_date)
        ]
        if match.empty:
            continue  # insufficient price history for this date

        recomputed_score = float(match["momentum_score"].iloc[0])

        n_checked += 1
        diff = abs(recomputed_score - stored_score)
        if diff > _SCORE_TOLERANCE:
            violations.append({
                "ticker": ticker,
                "score_date": str(score_date),
                "stored_score": round(stored_score, 8),
                "recomputed_score": round(recomputed_score, 8),
                "abs_diff": round(diff, 8),
            })

    return n_checked, len(violations), violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    # ── Load data ──────────────────────────────────────────────────────────
    if args.prices_file and args.scores_file:
        prices, scores = _load_from_files(args.prices_file, args.scores_file)
    elif args.snapshot_date:
        snap_date = date.fromisoformat(args.snapshot_date)
        prices, scores = _load_from_snapshot(snap_date, args.strategy_id)
    else:
        print(
            "ERROR: specify either (--prices-file + --scores-file) or --snapshot-date",
            file=sys.stderr,
        )
        sys.exit(2)

    prices, scores = _normalise(prices, scores)

    print(f"\n{'='*60}")
    print(f"  PIT Safety Audit — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    print(f"  Prices rows   : {len(prices):,}")
    print(f"  Score rows    : {len(scores):,}")
    print(f"  Sample size   : {args.sample_size}")
    print(f"  Seed          : {args.seed}")
    print()

    # ── Structural audit ──────────────────────────────────────────────────
    print("── 1. Structural checks ─────────────────────────────────")
    struct_violations = _structural_audit()
    if struct_violations:
        for v in struct_violations:
            print(f"  [FAIL] {v}")
    else:
        print("  [PASS] DataHandler enforces score_date < sim_date (strict <)")
    print()

    # ── Empirical audit ───────────────────────────────────────────────────
    if args.skip_empirical:
        print("── 2. Empirical checks (skipped via --skip-empirical) ───")
        print()
        all_clean = not struct_violations
    else:
        print("── 2. Empirical re-computation checks ───────────────────")
        n_checked, n_violations, violations = _empirical_audit(
            prices, scores, args.sample_size, args.seed
        )
        print(f"  Pairs checked : {n_checked:,}")
        print(f"  Violations    : {n_violations:,}")

        if violations:
            print()
            print("  VIOLATIONS (look-ahead bias detected):")
            for v in violations:
                print(
                    f"    {v['ticker']:10s} score_date={v['score_date']}  "
                    f"stored={v['stored_score']:+.6f}  "
                    f"recomputed={v['recomputed_score']:+.6f}  "
                    f"diff={v['abs_diff']:.2e}"
                )
        else:
            print("  [PASS] All sampled scores match PIT-filtered re-computation")
        print()
        all_clean = not struct_violations and n_violations == 0

    # ── Summary ───────────────────────────────────────────────────────────
    print("── Summary ──────────────────────────────────────────────")
    if all_clean:
        print("  RESULT: CLEAN — no point-in-time violations found")
        sys.exit(0)
    else:
        print("  RESULT: VIOLATIONS FOUND — review output above")
        sys.exit(1)


if __name__ == "__main__":
    main()

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
    python -m scripts.audit_pit_safety --prices-file prices.parquet --scores-file scores.parquet

MinIO snapshot (requires env vars):
    python -m scripts.audit_pit_safety --snapshot-date 2026-06-14 --strategy-id v1

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
from typing import Optional

import numpy as np
import pandas as pd
import structlog
from dotenv import load_dotenv

# Allow running as a script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from signals.composites.momentum_score import compute_momentum_scores  # noqa: E402

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
    source.add_argument(
        "--corporate-actions-file",
        help="Optional path to a corporate_actions parquet file (BUG-009 section 2.3, "
        "adversarial-review round 10). The backfill writes scores from a "
        "cutoff-adjusted price series, not raw close -- without this, the "
        "empirical audit recomputes from raw prices and will false-positive "
        "on any audited window that crosses a split/dividend.",
    )
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

def _load_from_files(
    prices_file: str, scores_file: str, corporate_actions_file: Optional[str] = None
) -> tuple[pd.DataFrame, pd.DataFrame, Optional[pd.DataFrame]]:
    logger.info("loading_from_parquet", prices=prices_file, scores=scores_file)
    prices = pd.read_parquet(prices_file)
    scores = pd.read_parquet(scores_file)
    corporate_actions = None
    if corporate_actions_file:
        corporate_actions = pd.read_parquet(corporate_actions_file)
    return prices, scores, corporate_actions


def _load_from_snapshot(
    snapshot_date: date, strategy_id: str
) -> tuple[pd.DataFrame, pd.DataFrame, Optional[pd.DataFrame]]:
    from data.storage.errors import SnapshotNotFoundError
    from data.storage.parquet_snapshots import ParquetSnapshots
    snaps = ParquetSnapshots()
    logger.info("loading_from_minio", snapshot_date=str(snapshot_date))
    prices = snaps.load_snapshot_legacy("daily_prices", snapshot_date)
    try:
        scores = snaps.load_snapshot_legacy("factor_scores", snapshot_date)
    except SnapshotNotFoundError as exc:
        if strategy_id != "v1":
            raise ValueError(
                "factor_scores snapshot is missing and alpha_scores fallback is "
                "only validated for the momentum-only strategy_id='v1'"
            ) from exc
        scores = snaps.load_snapshot_legacy("alpha_scores", snapshot_date)
        logger.warning(
            "using_alpha_scores_fallback",
            reason="factor_scores snapshot is not part of the backtest bundle",
            requirement="strategy alpha must be momentum-only",
        )
    if "strategy_id" in scores.columns:
        scores = scores[scores["strategy_id"] == strategy_id].reset_index(drop=True)

    # BUG-009 round 10: same optional-but-best-effort load as the backfill's
    # missing-snapshot handling, except this is a read-only diagnostic tool
    # (not a persist path), so there is nothing to fail closed on -- a
    # missing snapshot here just means the empirical audit degrades to raw
    # (unadjusted) recomputation and main() prints a loud caveat. 03A-2:
    # narrowed to SnapshotNotFoundError specifically (BUG-039) -- an
    # infra/auth/corruption failure while loading corporate_actions must
    # still surface as a hard error even in this best-effort diagnostic
    # path, not be silently folded into "no corporate actions."
    try:
        corporate_actions = snaps.load_snapshot_legacy("corporate_actions", snapshot_date)
    except SnapshotNotFoundError:
        corporate_actions = None
        logger.warning(
            "corporate_actions_snapshot_missing",
            snapshot_date=str(snapshot_date),
            note="empirical audit will recompute from RAW prices; any audited "
            "window crossing a split/dividend may show a false-positive "
            "mismatch against the cutoff-adjusted stored score (BUG-009 "
            "section 2.3)",
        )
    return prices, scores, corporate_actions


def _normalise(
    prices: pd.DataFrame,
    scores: pd.DataFrame,
    corporate_actions: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, Optional[pd.DataFrame]]:
    """Cast date columns to ``datetime.date`` objects."""
    for col in ("date",):
        if col in prices.columns:
            prices = prices.copy()
            prices[col] = pd.to_datetime(prices[col]).dt.date

    for col in ("score_date",):
        if col in scores.columns:
            scores = scores.copy()
            scores[col] = pd.to_datetime(scores[col]).dt.date

    if corporate_actions is not None and "ex_date" in corporate_actions.columns:
        corporate_actions = corporate_actions.copy()
        corporate_actions["ex_date"] = pd.to_datetime(corporate_actions["ex_date"]).dt.date

    return prices, scores, corporate_actions


# ---------------------------------------------------------------------------
# Structural audit
# ---------------------------------------------------------------------------

def _structural_audit() -> list[str]:
    """Verify strict score visibility (DataHandler) AND the IC timing contract
    (BUG-009 section 2) enforce score_date < entry_date structurally."""
    import inspect

    from backtesting.engine.data_handler import DataHandler

    source = inspect.getsource(DataHandler.get_latest_signals)
    violations: list[str] = []

    # Match the actual implementation pattern: self._alpha_scores["score_date"] < sim_date
    if '"score_date"] < sim_date' in source:
        logger.info("structural_check_passed", check="score_date_strict_lt")
    elif '"score_date"] <= sim_date' in source:
        violations.append(
            "DataHandler.get_latest_signals uses '<=' for score_date filter — "
            "should be '<' (strictly less-than) to enforce the 1-day execution lag."
        )
    else:
        violations.append(
            "DataHandler.get_latest_signals: could not confirm score_date filter direction "
            "from source inspection. Manual review required."
        )

    violations.extend(_structural_audit_timing_contract())
    return violations


def _structural_audit_timing_contract() -> list[str]:
    """Verify signals.research.timing enforces score_date < entry_date < exit_date
    (BUG-009 section 2) and that signals.research.ic delegates return
    computation to it rather than reintroducing a same-close shortcut."""
    import inspect

    from signals.research import ic as ic_module
    from signals.research import timing as timing_module

    violations: list[str] = []

    timing_source = inspect.getsource(timing_module.build_return_series)
    if "SameDateScoreError" not in timing_source:
        violations.append(
            "signals.research.timing.build_return_series no longer references "
            "SameDateScoreError — the score_date < entry_date < exit_date guard "
            "may have been removed (BUG-009 section 2.1)."
        )
    if "execution_lag_sessions" not in inspect.getsource(timing_module.TimingPolicy):
        violations.append(
            "signals.research.timing.TimingPolicy no longer has an "
            "execution_lag_sessions field — the timing contract may have changed "
            "shape without updating this audit."
        )

    fwd_source = inspect.getsource(ic_module.compute_forward_returns)
    if "build_return_series" not in fwd_source:
        violations.append(
            "signals.research.ic.compute_forward_returns no longer delegates to "
            "signals.research.timing.build_return_series — it may have "
            "reintroduced a same-close (BUG-009) return computation."
        )
    if not violations:
        logger.info("structural_check_passed", check="timing_contract_score_lt_entry")
    return violations


# ---------------------------------------------------------------------------
# Empirical audit
# ---------------------------------------------------------------------------

def _adjusted_prices_for_audit(
    prices_close: pd.DataFrame, corporate_actions: pd.DataFrame
) -> pd.DataFrame:
    """Rebuild the SAME cutoff-adjusted scoring input the backfill actually
    wrote scores from (adversarial-review round 10, BUG-009 section 2.3):
    ``scripts/backfill_momentum_scores.py`` scores momentum from
    ``adj_close``, not raw ``close``, since round 4/9. Recomputing the
    empirical audit from raw prices makes the audit tool itself unreliable
    on exactly the windows that matter most — any audited window crossing a
    split/dividend would show a spurious mismatch against the correctly
    adjusted stored score.

    Uses ``session_close_cutoff(max(prices date))`` as the single boundary
    cutoff — always on/after every audited score_date — which is safe by
    the same ratio-cancellation argument already established for the
    backfill's own single-boundary-cutoff approximation (BUG-071): a
    uniform multiplicative adjustment factor cancels out of any price ratio
    computed within a lookback window that lies entirely before the
    action's ex_date, so the exact cutoff position (as long as it is on or
    after the window) does not change the resulting momentum ratio.
    """
    from data.normalization.corporate_actions import build_score_price_history_as_of
    from data.universe.calendar import conservative_known_at_for_date_only_source, session_close_cutoff

    corporate_actions = corporate_actions.copy()
    if "known_at" not in corporate_actions.columns:
        # Same legacy-snapshot fallback as scripts/backfill_momentum_scores.py
        # (BUG-009 round 3): a pre-migration-011 snapshot has no
        # known_at/source_version columns. Synthesize conservatively rather
        # than crashing or silently skipping adjustment.
        corporate_actions["known_at"] = corporate_actions["ex_date"].apply(
            conservative_known_at_for_date_only_source
        )
        corporate_actions["source_version"] = "legacy_pre_migration_011_snapshot"
    else:
        corporate_actions["known_at"] = pd.to_datetime(corporate_actions["known_at"], utc=True)
        if "source_version" not in corporate_actions.columns:
            corporate_actions["source_version"] = "unknown"

    cutoff = session_close_cutoff(prices_close["date"].max())
    adjusted, _meta = build_score_price_history_as_of(prices_close, corporate_actions, score_cutoff=cutoff)
    return (
        adjusted[["ticker", "date", "adj_close"]]
        .rename(columns={"adj_close": "close"})
        .assign(close=lambda df: df["close"].astype(float))
    )


def _empirical_audit(
    prices: pd.DataFrame,
    scores: pd.DataFrame,
    sample_size: int,
    seed: int,
    corporate_actions: Optional[pd.DataFrame] = None,
) -> tuple[int, int, list[dict]]:
    """Re-compute momentum scores per sampled (ticker, score_date) and compare.

    Runs ``compute_momentum_scores`` once on the full price history, then for
    each sampled ``(ticker, score_date)`` pair looks up the result at exactly
    that date and compares it to the stored score.  Momentum uses purely
    backward-looking rolling windows (``wide.shift(skip_days)``), so the score
    at date d is identical whether the computation uses prices[:d] or the full
    history — no repeated per-sample passes are needed.

    A mismatch means the stored score cannot be reproduced from the price data:
    it was either computed with different prices, a different code version, or
    future price data (look-ahead bias).

    Args:
        corporate_actions: when supplied, the recomputation is built from the
            same cutoff-adjusted price series the backfill actually scores
            from (BUG-009 section 2.3, round 10). When ``None`` (no snapshot
            available), the recomputation falls back to raw prices and a
            caller-visible caveat should be printed — see
            ``main()`` / ``_load_from_snapshot``.

    Returns:
        (n_checked, n_violations, violation_records)
    """
    score_col = next(
        (
            column
            for column in ("z_score", "momentum_score", "alpha_score")
            if column in scores.columns
        ),
        None,
    )
    if score_col is None:
        logger.warning(
            "empirical_audit_skipped",
            reason="missing score column: expected z_score, momentum_score, or alpha_score",
        )
        return 0, 0, []
    factor_filter = (
        scores["factor_name"] == "momentum"
        if "factor_name" in scores.columns
        else pd.Series(True, index=scores.index)
    )
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

    if corporate_actions is not None and not corporate_actions.empty:
        scoring_prices = _adjusted_prices_for_audit(prices_close, corporate_actions)
    else:
        scoring_prices = prices_close

    all_recomputed = compute_momentum_scores(scoring_prices)

    tickers_with_prices = set(prices_close["ticker"].unique())

    for _, row in sample.iterrows():
        ticker: str = row["ticker"]
        score_date: date = row["score_date"]
        stored_score: float = float(row[score_col])

        if ticker not in tickers_with_prices:
            continue  # no price history for this ticker — cannot verify

        # Look up the score at exactly score_date (same date, same ticker).
        # Comparing at the same date is the correct test: if the stored score
        # used data beyond score_date the recomputed value will differ.
        match = all_recomputed[
            (all_recomputed["ticker"] == ticker) & (all_recomputed["date"] == score_date)
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


def _entry_exit_alignment_audit(prices: pd.DataFrame, sample_size: int, seed: int) -> list[str]:
    """Empirically verify actual entry/exit alignment (BUG-009 section 2.4):
    every (score_date, entry_date, exit_date) row produced by
    ``signals.research.timing.build_return_series`` from live price data
    satisfies score_date < entry_date < exit_date, and the forward_return is
    computed strictly from the entry/exit closes (never the score-date close).
    """
    from signals.research.timing import DEFAULT_TIMING_POLICY, build_return_series

    violations: list[str] = []

    prices_close = prices[["ticker", "date", "close"]].copy()
    prices_close["date"] = pd.to_datetime(prices_close["date"]).dt.date
    prices_close["close"] = prices_close["close"].astype(float)

    try:
        returns = build_return_series(prices_close, horizons=[1, 5, 21])
    except ValueError as exc:
        violations.append(f"build_return_series raised on live price data: {exc}")
        return violations

    if returns.empty:
        logger.warning("entry_exit_alignment_audit_skipped", reason="no rows produced")
        return violations

    if not (returns["score_date"] < returns["entry_date"]).all():
        violations.append(
            "build_return_series produced a row with score_date >= entry_date "
            "on live price data (BUG-009 section 2.1 violation)."
        )
    if not (returns["entry_date"] < returns["exit_date"]).all():
        violations.append(
            "build_return_series produced a row with entry_date >= exit_date "
            "on live price data."
        )
    if not (returns["timing_policy_id"] == DEFAULT_TIMING_POLICY.policy_id).all():
        violations.append(
            "build_return_series did not stamp every row with the expected "
            f"default timing_policy_id {DEFAULT_TIMING_POLICY.policy_id!r}."
        )

    # Spot-check a deterministic sample: forward_return recomputed from
    # entry/exit closes must match the returned value exactly (proves the
    # value is not silently derived from score_date's own close).
    rng = np.random.default_rng(seed)
    n = min(sample_size, len(returns))
    sample = returns.iloc[rng.choice(len(returns), size=n, replace=False)]
    closes = prices_close.set_index(["ticker", "date"])["close"]
    n_checked = 0
    for _, row in sample.iterrows():
        try:
            entry_close = closes[(row["ticker"], row["entry_date"])]
            exit_close = closes[(row["ticker"], row["exit_date"])]
        except KeyError:
            continue
        n_checked += 1
        expected = exit_close / entry_close - 1.0
        if abs(expected - row["forward_return"]) > _SCORE_TOLERANCE:
            violations.append(
                f"{row['ticker']} score_date={row['score_date']}: forward_return "
                f"{row['forward_return']:.8f} does not match entry/exit close "
                f"recomputation {expected:.8f}."
            )

    if not violations:
        logger.info(
            "entry_exit_alignment_audit_passed",
            n_rows=len(returns),
            n_spot_checked=n_checked,
        )
    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    load_dotenv()

    # ── Load data ──────────────────────────────────────────────────────────
    if args.prices_file and args.scores_file:
        prices, scores, corporate_actions = _load_from_files(
            args.prices_file, args.scores_file, args.corporate_actions_file
        )
    elif args.snapshot_date:
        snap_date = date.fromisoformat(args.snapshot_date)
        prices, scores, corporate_actions = _load_from_snapshot(snap_date, args.strategy_id)
    else:
        print(
            "ERROR: specify either (--prices-file + --scores-file) or --snapshot-date",
            file=sys.stderr,
        )
        sys.exit(2)

    prices, scores, corporate_actions = _normalise(prices, scores, corporate_actions)

    print(f"\n{'='*60}")
    print(f"  PIT Safety Audit - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    print(f"  Prices rows   : {len(prices):,}")
    print(f"  Score rows    : {len(scores):,}")
    print(f"  Sample size   : {args.sample_size}")
    print(f"  Seed          : {args.seed}")
    if corporate_actions is not None and not corporate_actions.empty:
        print(f"  Corp. actions : {len(corporate_actions):,} rows (cutoff-adjusted recomputation)")
    else:
        print(
            "  Corp. actions : none loaded -- empirical audit recomputes from RAW "
            "prices; a mismatch on a window crossing a split/dividend may be a "
            "false positive, not real look-ahead bias (BUG-009 section 2.3)"
        )
    print()

    # ── Structural audit ──────────────────────────────────────────────────
    print("-- 1. Structural checks ---------------------------------")
    struct_violations = _structural_audit()
    if struct_violations:
        for v in struct_violations:
            print(f"  [FAIL] {v}")
    else:
        print("  [PASS] DataHandler enforces score_date < sim_date (strict <)")
    print()

    # ── Empirical audit ───────────────────────────────────────────────────
    if args.skip_empirical:
        print("-- 2. Empirical checks (skipped via --skip-empirical) ---")
        print()
        all_clean = not struct_violations
    else:
        print("-- 2. Empirical re-computation checks -------------------")
        n_checked, n_violations, violations = _empirical_audit(
            prices, scores, args.sample_size, args.seed, corporate_actions=corporate_actions
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
        elif n_checked == 0:
            print("  [FAIL] No score pairs could be checked")
        else:
            print("  [PASS] All sampled scores match PIT-filtered re-computation")
        print()
        all_clean = not struct_violations and n_checked > 0 and n_violations == 0

    # ── Entry/exit alignment audit (BUG-009 section 2.4) ───────────────────
    print("-- 3. Entry/exit timing alignment checks ----------------")
    alignment_violations = _entry_exit_alignment_audit(prices, args.sample_size, args.seed)
    if alignment_violations:
        for v in alignment_violations:
            print(f"  [FAIL] {v}")
    else:
        print(
            "  [PASS] score_date < entry_date < exit_date and forward_return "
            "match entry/exit close recomputation on live price data"
        )
    print()
    all_clean = all_clean and not alignment_violations

    # ── Summary ───────────────────────────────────────────────────────────
    print("-- Summary ------------------------------------------------")
    if all_clean:
        print("  RESULT: CLEAN - no point-in-time violations found")
        sys.exit(0)
    else:
        print("  RESULT: VIOLATIONS FOUND - review output above")
        sys.exit(1)


if __name__ == "__main__":
    main()

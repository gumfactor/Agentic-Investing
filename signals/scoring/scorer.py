"""Composite alpha scorer.

Combines individual factor scores into a single alpha_score per (ticker, date).

Design
------
The scorer is a pure function: it takes already-computed factor DataFrames and
returns combined output DataFrames.  It does not read from or write to the DB —
the caller (Airflow task) handles I/O.  This makes unit testing straightforward.

Factor score contract
---------------------
Each factor DataFrame passed to combine_factor_scores must be long-format:
    ticker, date, {composite_col}, [sub-factor columns...]

The composite_col is the column used for the final alpha blend (e.g.
'momentum_score', 'lowvol_score').  Sub-factor columns are written to
factor_scores for diagnostics but not used in the alpha blend.

Equal-weight blending
---------------------
When weights=None, each factor with at least one non-NaN score for a
(date, ticker) gets weight 1 / n_available.  This gracefully handles the
case where fundamentals are not yet loaded (value, quality are absent) —
the price-based factors (momentum, low-vol) carry full weight.

Weighted blending
-----------------
When weights are provided, per-row weight renormalisation is applied so a
ticker missing one factor is not penalised: the weights of available factors
are rescaled to sum to 1 for that row only.  This mirrors the equal-weight
skipna=True behaviour.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


def combine_factor_scores(
    factor_scores: dict[str, pd.DataFrame],
    score_col_map: dict[str, str],
    strategy_id: str,
    score_date: Optional[date] = None,
    weights: Optional[dict[str, float]] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Combine per-factor score DataFrames into factor_scores and alpha_scores.

    Args:
        factor_scores: Mapping of factor_name → long-format DataFrame containing
            at minimum ``ticker``, ``date``, and the column named in
            score_col_map[factor_name].
        score_col_map: Mapping of factor_name → name of the composite score
            column within that factor's DataFrame (e.g. ``'momentum_score'``).
        strategy_id: Strategy config version identifier (stored in DB).
        score_date: If provided, restrict output to this date only.  When None,
            all dates in the factor DataFrames flow through; use with care for
            large multi-year backtests (large XCom payloads).
        weights: Optional dict of factor_name → weight for blending.
            Weights do not need to sum to 1; they are normalised per-row to
            handle missing factors gracefully.  If None, equal-weight blending
            across available factors is used.

    Returns:
        A tuple of two DataFrames:
          factor_scores_df — long-format rows for the ``factor_scores`` table:
              ticker, score_date, factor_name, strategy_id, z_score, raw_value
          alpha_scores_df  — rows for the ``alpha_scores`` table:
              ticker, score_date, strategy_id, alpha_score, rank, universe_size
    """
    if not factor_scores:
        empty_f = pd.DataFrame(
            columns=["ticker", "score_date", "factor_name", "strategy_id", "z_score", "raw_value"]
        )
        empty_a = pd.DataFrame(
            columns=["ticker", "score_date", "strategy_id", "alpha_score", "rank", "universe_size"]
        )
        return empty_f, empty_a

    # ── Build factor_scores_df (vectorised) ──────────────────────────────────
    factor_chunks: list[pd.DataFrame] = []
    for factor_name, df in factor_scores.items():
        if df.empty:
            continue
        composite_col = score_col_map.get(factor_name)
        if composite_col is None or composite_col not in df.columns:
            logger.warning(
                "scorer_missing_composite_col",
                factor=factor_name,
                expected=composite_col,
                available=list(df.columns),
            )
            continue

        subset = df if score_date is None else df[df["date"] == score_date]
        chunk = subset[["ticker", "date", composite_col]].copy()
        chunk = chunk.rename(columns={"date": "score_date", composite_col: "z_score"})
        chunk["factor_name"] = factor_name
        chunk["strategy_id"] = strategy_id
        chunk["raw_value"] = None
        factor_chunks.append(chunk[["ticker", "score_date", "factor_name", "strategy_id", "z_score", "raw_value"]])

    if not factor_chunks:
        empty_f = pd.DataFrame(
            columns=["ticker", "score_date", "factor_name", "strategy_id", "z_score", "raw_value"]
        )
        return empty_f, pd.DataFrame(
            columns=["ticker", "score_date", "strategy_id", "alpha_score", "rank", "universe_size"]
        )

    factor_scores_df = pd.concat(factor_chunks, ignore_index=True)

    # ── Pivot to wide for composite computation ───────────────────────────────
    wide = (
        factor_scores_df[["ticker", "score_date", "factor_name", "z_score"]]
        .pivot_table(index=["score_date", "ticker"], columns="factor_name", values="z_score")
        .reset_index()
    )
    wide.columns.name = None
    factor_cols = [c for c in wide.columns if c not in ("score_date", "ticker")]

    if not factor_cols:
        return factor_scores_df, pd.DataFrame(
            columns=["ticker", "score_date", "strategy_id", "alpha_score", "rank", "universe_size"]
        )

    # ── Compute weighted alpha score ──────────────────────────────────────────
    if weights is None:
        # Equal weight across factors that have a non-NaN score for each row
        wide["alpha_score"] = wide[factor_cols].mean(axis=1, skipna=True)
    else:
        # Per-row weight renormalisation: missing factors are excluded from the
        # denominator so a ticker with one missing factor is not penalised.
        available = [f for f in factor_cols if f in weights]
        if not available:
            logger.warning("scorer_no_weight_match", factor_cols=factor_cols, weights=list(weights))
            wide["alpha_score"] = wide[factor_cols].mean(axis=1, skipna=True)
        else:
            w = np.array([weights[f] for f in available], dtype=float)
            vals = wide[available].values  # (N, F)
            mask = ~np.isnan(vals)         # True where factor value exists
            w_active = np.where(mask, w, 0.0)            # zero weight for missing
            row_w_sum = w_active.sum(axis=1)              # sum of active weights per row
            weighted_sum = np.nansum(vals * w, axis=1)    # weighted numerator
            wide["alpha_score"] = np.where(row_w_sum > 0, weighted_sum / row_w_sum, np.nan)

    wide["strategy_id"] = strategy_id

    # ── Build alpha_scores_df: drop NaN first, then rank within clean set ─────
    alpha_scores_df = (
        wide[["ticker", "score_date", "strategy_id", "alpha_score"]]
        .dropna(subset=["alpha_score"])
        .reset_index(drop=True)
    )

    if not alpha_scores_df.empty:
        alpha_scores_df["rank"] = (
            alpha_scores_df.groupby("score_date")["alpha_score"]
            .rank(ascending=False, method="first")
            .astype("Int64")
        )
        alpha_scores_df["universe_size"] = (
            alpha_scores_df.groupby("score_date")["ticker"].transform("count")
        )
    else:
        alpha_scores_df["rank"] = pd.array([], dtype="Int64")
        alpha_scores_df["universe_size"] = pd.Series(dtype="int64")

    logger.info(
        "composite_scores_computed",
        strategy_id=strategy_id,
        factors=factor_cols,
        score_dates=alpha_scores_df["score_date"].nunique(),
        tickers=alpha_scores_df["ticker"].nunique(),
    )
    return factor_scores_df, alpha_scores_df

"""Short-Term Reversal composite signal.

Combines three short-horizon mean-reversion signals to identify stocks that
have sold off sharply in the recent days-to-weeks window and are candidates
for a price snapback.

The thesis: over very short horizons (1 day to 1 month) stock returns
exhibit mean-reversion rather than momentum, driven by market microstructure
effects — temporary order imbalances, liquidity-driven selling, and end-of-
month rebalancing flows. Jegadeesh (1990) documents 1-month reversal that
is economically large; Lehmann (1990) shows analogous 1-week reversal.
This composite is distinct from the medium-to-long-term contrarian signals
in Group 4 (Quality Dip, Deep Value Oversold): those strategies require
fundamental cheapness as an anchor; this composite is purely price-driven
and is intended for short-duration mean-reversion strategies.

The three signals are complementary:
  (a) reversal_1m: negated 21-day return — recent 1-month loser scores
      higher. The primary reversal signal.
  (b) reversal_1w: negated 5-day return — captures even more recent selling
      pressure that may resolve over days. Provides a fresher read on the
      current supply/demand imbalance.
  (c) bb_z_score_20: Bollinger Band Z-score (price vs. 20-day mean in
      standard deviation units), negated internally — stocks furthest
      below their 20-day mean score highest, consistent with the
      mean-reversion thesis that prices tend to return to short-term
      equilibrium.

The reversal_1m and reversal_1w signals are already negated at the indicator
level (recent losers = higher score). The bb_z_score_20 is negated
internally (higher raw value = above mean = not a reversal candidate).
The original bb_z_score_20 value is preserved in the output.

Basis: Jegadeesh (1990) "Evidence of Predictable Behavior of Security
Returns"; Lehmann (1990) "Fads, Martingales, and Market Efficiency";
Lo & MacKinlay (1990) on contrarian portfolio profits.

Default weighting: 1-month reversal 50%, 1-week reversal 30%, inverted
Bollinger z-score 20%. The 1-month signal is the most documented; 1-week
adds recency; the Bollinger filter adds a structural price-distance screen.

Inputs
------
reversal_1m_scores: Output of compute_reversal_1m_scores(). Must contain
    ``ticker``, ``date``, ``reversal_1m_score``.
reversal_1w_scores: Output of compute_reversal_1w_scores(). Must contain
    ``ticker``, ``date``, ``reversal_1w_score``.
bb_z_score_scores: Output of compute_bb_z_score_20_scores(). Must contain
    ``ticker``, ``date``, ``bb_z_score_20_score``.

Output
------
short_term_reversal_score: weighted blend, cross-sectionally re-standardized
per date. Higher = recent 1-month and 1-week loser + price furthest below
its 20-day average (strong mean-reversion candidate).
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_short_term_reversal_scores(
    reversal_1m_scores: pd.DataFrame,
    reversal_1w_scores: pd.DataFrame,
    bb_z_score_scores: pd.DataFrame,
    reversal_1m_weight: float = 0.50,
    reversal_1w_weight: float = 0.30,
    bb_z_score_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend 1-month reversal, 1-week reversal, and inverted Bollinger z-score.

    Args:
        reversal_1m_scores: Output of compute_reversal_1m_scores(). Must
            contain columns ``ticker``, ``date``, ``reversal_1m_score``.
        reversal_1w_scores: Output of compute_reversal_1w_scores(). Must
            contain columns ``ticker``, ``date``, ``reversal_1w_score``.
        bb_z_score_scores: Output of compute_bb_z_score_20_scores(). Must
            contain columns ``ticker``, ``date``, ``bb_z_score_20_score``.
        reversal_1m_weight: Relative weight for 1-month reversal. Default 0.50.
        reversal_1w_weight: Relative weight for 1-week reversal. Default 0.30.
        bb_z_score_weight: Relative weight for the Bollinger z-score signal
            (inverted). Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``reversal_1m_score``,
            ``reversal_1w_score``, ``bb_z_score_20_score``,
            ``short_term_reversal_score``

        ``bb_z_score_20_score`` is negated internally so that stocks
        furthest below their 20-day mean contribute a higher score. The
        original raw value is preserved in the output column.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(reversal_1m_scores, "reversal_1m_scores", ["reversal_1m_score"])
    _validate(reversal_1w_scores, "reversal_1w_scores", ["reversal_1w_score"])
    _validate(bb_z_score_scores, "bb_z_score_scores", ["bb_z_score_20_score"])

    merged = (
        reversal_1m_scores[["ticker", "date", "reversal_1m_score"]]
        .merge(
            reversal_1w_scores[["ticker", "date", "reversal_1w_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            bb_z_score_scores[["ticker", "date", "bb_z_score_20_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    # Negate bb_z_score: lower raw value = price further below 20-day mean = stronger reversal candidate
    merged = merged.copy()
    merged["_below_mean"] = -merged["bb_z_score_20_score"]

    weights = {
        "reversal_1m_score": reversal_1m_weight,
        "reversal_1w_score": reversal_1w_weight,
        "_below_mean": bb_z_score_weight,
    }
    result = blend_scores(merged, weights, "short_term_reversal_score")

    result = result.drop(columns=["_below_mean"])
    result = result.dropna(subset=["short_term_reversal_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "short_term_reversal_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        reversal_1m_weight=reversal_1m_weight,
        reversal_1w_weight=reversal_1w_weight,
        bb_z_score_weight=bb_z_score_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

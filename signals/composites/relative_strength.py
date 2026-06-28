"""Relative Strength composite signal.

Combines multi-horizon relative performance versus the broad market with a
long-term trend quality filter to identify stocks that are persistently
leading the market — not just temporary bounces.

The thesis: institutional capital flows toward sector and stock leaders.
A stock that outperforms the S&P 500 over both 12 months and 3 months is
exhibiting persistent leadership rather than a single-quarter spike; adding
a rising 200-day MA slope filter confirms the structural trend is intact
rather than a mean-reversion setup in a downtrend. This composite is the
quantitative backbone of sector rotation and relative-momentum strategies.

The three signals span different time horizons and structural contexts:
  (a) rel_strength_vs_spy_12m: 12-month return minus SPY return (skipping
      the last month). Long-horizon relative leadership. The single most
      reliable cross-sectional predictor of near-term outperformance after
      controlling for market beta.
  (b) rel_strength_vs_spy_3m: 3-month relative return. Captures whether
      the leadership has continued in the more recent window. A stock that
      led 12 months ago but lagged the last 3 months may be losing
      institutional support.
  (c) ma_slope_200: rate of change of the 200-day SMA normalised by price.
      A rising 200-day MA confirms the long-term structural trend is
      constructive, distinguishing persistent leaders from oversold bounces
      in broken downtrends.

All three signals are higher = stronger; no negation is required.

Basis: Jegadeesh & Titman (1993) on cross-sectional momentum; Grinblatt &
Moskowitz (2004) on the persistence of winner/loser effects; practitioner
literature on 52-week relative strength as an institutional screening tool.

Default weighting: 12m relative strength 50%, 3m relative strength 30%,
MA slope 20%. Long-horizon relative outperformance carries the most weight;
short-horizon confirmation and structural filter are secondary.

Inputs
------
rel_strength_12m_scores: Output of compute_rel_strength_vs_spy_12m_scores().
    Must contain ``ticker``, ``date``, ``rel_strength_vs_spy_12m_score``.
rel_strength_3m_scores: Output of compute_rel_strength_vs_spy_3m_scores().
    Must contain ``ticker``, ``date``, ``rel_strength_vs_spy_3m_score``.
ma_slope_scores: Output of compute_ma_slope_200_scores(). Must contain
    ``ticker``, ``date``, ``ma_slope_200_score``.

Output
------
relative_strength_score: weighted blend, cross-sectionally re-standardized
per date. Higher = consistent multi-horizon outperformance vs. market +
rising long-term structural trend.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_relative_strength_scores(
    rel_strength_12m_scores: pd.DataFrame,
    rel_strength_3m_scores: pd.DataFrame,
    ma_slope_scores: pd.DataFrame,
    rel_strength_12m_weight: float = 0.50,
    rel_strength_3m_weight: float = 0.30,
    ma_slope_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend 12m and 3m relative strength vs SPY with 200-day MA slope.

    Args:
        rel_strength_12m_scores: Output of
            compute_rel_strength_vs_spy_12m_scores(). Must contain columns
            ``ticker``, ``date``, ``rel_strength_vs_spy_12m_score``.
        rel_strength_3m_scores: Output of
            compute_rel_strength_vs_spy_3m_scores(). Must contain columns
            ``ticker``, ``date``, ``rel_strength_vs_spy_3m_score``.
        ma_slope_scores: Output of compute_ma_slope_200_scores(). Must
            contain columns ``ticker``, ``date``, ``ma_slope_200_score``.
        rel_strength_12m_weight: Relative weight for 12-month RS. Default 0.50.
        rel_strength_3m_weight: Relative weight for 3-month RS. Default 0.30.
        ma_slope_weight: Relative weight for 200-day MA slope. Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``rel_strength_vs_spy_12m_score``,
            ``rel_strength_vs_spy_3m_score``, ``ma_slope_200_score``,
            ``relative_strength_score``

        All three signals point in the same direction (higher = stronger
        relative leadership); no internal negation is required.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(rel_strength_12m_scores, "rel_strength_12m_scores", ["rel_strength_vs_spy_12m_score"])
    _validate(rel_strength_3m_scores, "rel_strength_3m_scores", ["rel_strength_vs_spy_3m_score"])
    _validate(ma_slope_scores, "ma_slope_scores", ["ma_slope_200_score"])

    merged = (
        rel_strength_12m_scores[["ticker", "date", "rel_strength_vs_spy_12m_score"]]
        .merge(
            rel_strength_3m_scores[["ticker", "date", "rel_strength_vs_spy_3m_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            ma_slope_scores[["ticker", "date", "ma_slope_200_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "rel_strength_vs_spy_12m_score": rel_strength_12m_weight,
        "rel_strength_vs_spy_3m_score": rel_strength_3m_weight,
        "ma_slope_200_score": ma_slope_weight,
    }
    result = blend_scores(merged, weights, "relative_strength_score")

    result = result.dropna(subset=["relative_strength_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "relative_strength_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        rel_strength_12m_weight=rel_strength_12m_weight,
        rel_strength_3m_weight=rel_strength_3m_weight,
        ma_slope_weight=ma_slope_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

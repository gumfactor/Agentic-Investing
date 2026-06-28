"""Trend Strength composite signal.

Combines three independent trend-confirmation signals into a single score.

The thesis: a price trend that is simultaneously structurally aligned (MA cross),
statistically clean (high log-price R²), and behaviorally consistent (high
daily up-fraction) is unlikely to be noise. Each signal measures trend quality
from a different angle; agreement across all three is strong evidence of a
genuine, durable trend.

Basis: Faber "A Quantitative Approach to Tactical Asset Allocation" (2007) for
MA-cross regime filters; Lo & MacKinlay (1988) on trend autocorrelation; the
practitioner observation that high-R² trends have lower reversal risk.

Default weighting: MA cross 40%, trend R² 30%, trend consistency 30%.
The MA cross carries the most structural weight because it captures the
long-term regime; R² and consistency are secondary confirmation filters.

Inputs
------
ma_cross_scores: Output of compute_ma_cross_50_200_scores(). Must contain
    ``ticker``, ``date``, ``ma_cross_50_200_score``.
trend_r2_scores: Output of compute_trend_r2_50d_scores(). Must contain
    ``ticker``, ``date``, ``trend_r2_50d_score``.
trend_consistency_scores: Output of compute_trend_consistency_63d_scores().
    Must contain ``ticker``, ``date``, ``trend_consistency_63d_score``.

Output
------
trend_strength_score: weighted blend, cross-sectionally re-standardized per
date. Higher = strong, clean, consistent uptrend across multiple timeframes.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_trend_strength_scores(
    ma_cross_scores: pd.DataFrame,
    trend_r2_scores: pd.DataFrame,
    trend_consistency_scores: pd.DataFrame,
    ma_cross_weight: float = 0.4,
    trend_r2_weight: float = 0.3,
    trend_consistency_weight: float = 0.3,
) -> pd.DataFrame:
    """Blend MA-cross, trend R², and trend consistency into a trend-strength composite.

    Args:
        ma_cross_scores: Output of compute_ma_cross_50_200_scores(). Must contain
            columns ``ticker``, ``date``, ``ma_cross_50_200_score``.
        trend_r2_scores: Output of compute_trend_r2_50d_scores(). Must contain
            columns ``ticker``, ``date``, ``trend_r2_50d_score``.
        trend_consistency_scores: Output of compute_trend_consistency_63d_scores().
            Must contain columns ``ticker``, ``date``, ``trend_consistency_63d_score``.
        ma_cross_weight: Relative weight for MA cross. Default 0.4.
        trend_r2_weight: Relative weight for trend R². Default 0.3.
        trend_consistency_weight: Relative weight for trend consistency. Default 0.3.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``ma_cross_50_200_score``,
            ``trend_r2_50d_score``, ``trend_consistency_63d_score``,
            ``trend_strength_score``

        Rows present in only one or two inputs are retained with NaN for the
        missing dimension(s); their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(ma_cross_scores, "ma_cross_scores", ["ma_cross_50_200_score"])
    _validate(trend_r2_scores, "trend_r2_scores", ["trend_r2_50d_score"])
    _validate(trend_consistency_scores, "trend_consistency_scores", ["trend_consistency_63d_score"])

    merged = (
        ma_cross_scores[["ticker", "date", "ma_cross_50_200_score"]]
        .merge(
            trend_r2_scores[["ticker", "date", "trend_r2_50d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            trend_consistency_scores[["ticker", "date", "trend_consistency_63d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "ma_cross_50_200_score": ma_cross_weight,
        "trend_r2_50d_score": trend_r2_weight,
        "trend_consistency_63d_score": trend_consistency_weight,
    }
    result = blend_scores(merged, weights, "trend_strength_score")
    result = result.dropna(subset=["trend_strength_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "trend_strength_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        ma_cross_weight=ma_cross_weight,
        trend_r2_weight=trend_r2_weight,
        trend_consistency_weight=trend_consistency_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

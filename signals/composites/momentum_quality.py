"""Momentum-Quality composite signal.

Weights momentum heavily and uses quality as a filter/stabilizer.

The thesis: momentum crashes concentrate in low-quality, financially distressed
firms whose momentum is fragile and mean-reverting. Overlaying a quality screen
preserves most of the momentum return while reducing crash risk, because
high-quality firms with momentum are in genuine uptrends rather than short
squeezes or speculative bubbles.

Basis: Novy-Marx "The Other Side of Value" (2012); Asness et al. "Fact, Fiction
and Momentum Investing" (2014); documented reduction in momentum drawdowns when
quality is used as a filter.

Default weighting: momentum 70%, quality 30%. Momentum is the primary signal;
quality is a secondary stabilizer, not a co-equal dimension. Strategies that
want equal weighting should set momentum_weight=quality_weight=0.5.

Inputs
------
Both inputs are expected to be the output DataFrames of compute_momentum_scores()
and compute_quality_scores() respectively — composite score columns already
cross-sectionally z-scored.

Output
------
momentum_quality_score: weighted blend, cross-sectionally re-standardized per
date. Higher = stronger price trend in a financially healthy company.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_momentum_quality_scores(
    momentum_scores: pd.DataFrame,
    quality_scores: pd.DataFrame,
    momentum_weight: float = 0.7,
    quality_weight: float = 0.3,
) -> pd.DataFrame:
    """Blend momentum_score and quality_score into a momentum-quality composite.

    Args:
        momentum_scores: Output of compute_momentum_scores(). Must contain
            columns ``ticker``, ``date``, ``momentum_score``.
        quality_scores: Output of compute_quality_scores(). Must contain
            columns ``ticker``, ``date``, ``quality_score``.
        momentum_weight: Relative weight for momentum. Default 0.7.
        quality_weight: Relative weight for quality. Default 0.3.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``momentum_score``, ``quality_score``,
            ``momentum_quality_score``

        Rows present in only one input are retained with NaN for the missing
        dimension; its weight is redistributed to the available signal.
        Rows where both inputs are NaN are dropped.
    """
    _validate(momentum_scores, "momentum_scores", ["momentum_score"])
    _validate(quality_scores, "quality_scores", ["quality_score"])

    merged = (
        momentum_scores[["ticker", "date", "momentum_score"]]
        .merge(
            quality_scores[["ticker", "date", "quality_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {"momentum_score": momentum_weight, "quality_score": quality_weight}
    result = blend_scores(merged, weights, "momentum_quality_score")
    result = result.dropna(subset=["momentum_quality_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "momentum_quality_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        momentum_weight=momentum_weight,
        quality_weight=quality_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

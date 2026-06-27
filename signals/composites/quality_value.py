"""Quality-Value composite signal.

Blends the value_score and quality_score composites into a single signal.
The thesis: value alone buys distressed companies; quality alone buys expensive
compounders. The intersection — cheap AND financially strong — is the most
durable form of alpha.

Basis: Asness, Frazzini & Pedersen "Quality Minus Junk" (2019); Graham-style
fundamental screening; widely replicated in systematic factor investing.

Inputs
------
Both inputs are expected to be the output DataFrames of compute_value_scores()
and compute_quality_scores() respectively — i.e., their composite score columns
are already cross-sectionally z-scored.

Output
------
quality_value_score: weighted blend of value_score and quality_score,
cross-sectionally re-standardized per date. Higher = cheaper AND higher quality.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)

_DEFAULT_WEIGHTS = {
    "value_score": 0.5,
    "quality_score": 0.5,
}


def compute_quality_value_scores(
    value_scores: pd.DataFrame,
    quality_scores: pd.DataFrame,
    value_weight: float = 0.5,
    quality_weight: float = 0.5,
) -> pd.DataFrame:
    """Blend value_score and quality_score into a quality-value composite.

    Args:
        value_scores: Output of compute_value_scores(). Must contain columns
            ``ticker``, ``date``, ``value_score``.
        quality_scores: Output of compute_quality_scores(). Must contain columns
            ``ticker``, ``date``, ``quality_score``.
        value_weight: Relative weight for the value dimension. Default 0.5.
        quality_weight: Relative weight for the quality dimension. Default 0.5.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``value_score``, ``quality_score``,
            ``quality_value_score``

        Rows present in only one input are retained with NaN for the missing
        dimension; its weight is redistributed to the available signal.
        Rows where both inputs are NaN are dropped.
    """
    _validate(value_scores, "value_scores", ["value_score"])
    _validate(quality_scores, "quality_scores", ["quality_score"])

    merged = (
        value_scores[["ticker", "date", "value_score"]]
        .merge(
            quality_scores[["ticker", "date", "quality_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {"value_score": value_weight, "quality_score": quality_weight}
    result = blend_scores(merged, weights, "quality_value_score")
    result = result.dropna(subset=["quality_value_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "quality_value_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        value_weight=value_weight,
        quality_weight=quality_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

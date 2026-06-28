"""Volume-Confirmed Momentum composite signal.

Blends price momentum with volume conviction signals to produce a more
durable momentum score.

The thesis: momentum crashes and false breakouts cluster in moves driven by
thin, low-conviction volume. Price momentum that is simultaneously confirmed
by volume-weighted returns (conviction-backed price action) and OBV trend
(institutional accumulation) is more likely to persist. Negatively, price
rising on declining volume is a warning sign captured by the volume signals
pulling the composite score down.

Basis: Blume, Easley & O'Hara "Market Statistics and Technical Analysis"
(1994); Granville OBV theory; Buff Dormeier "Investing with Volume Analysis"
(2011); widely cited in systematic momentum literature that volume is a
useful filter for reducing momentum crash severity.

Default weighting: momentum_score 50%, volume_weighted_momentum_21d 30%,
obv_momentum_21d 20%. Raw momentum is the primary signal; volume signals
are confirmation filters that increase weight on high-conviction moves.

Inputs
------
momentum_scores: Output of compute_momentum_scores(). Must contain
    ``ticker``, ``date``, ``momentum_score``.
volume_weighted_momentum_scores: Output of
    compute_volume_weighted_momentum_21d_scores(). Must contain
    ``ticker``, ``date``, ``volume_weighted_momentum_21d_score``.
obv_momentum_scores: Output of compute_obv_momentum_21d_scores(). Must
    contain ``ticker``, ``date``, ``obv_momentum_21d_score``.

Output
------
volume_momentum_score: weighted blend, cross-sectionally re-standardized per
date. Higher = strong price momentum backed by volume conviction.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_volume_momentum_scores(
    momentum_scores: pd.DataFrame,
    volume_weighted_momentum_scores: pd.DataFrame,
    obv_momentum_scores: pd.DataFrame,
    momentum_weight: float = 0.5,
    volume_weighted_momentum_weight: float = 0.3,
    obv_momentum_weight: float = 0.2,
) -> pd.DataFrame:
    """Blend momentum, volume-weighted momentum, and OBV momentum into a composite.

    Args:
        momentum_scores: Output of compute_momentum_scores(). Must contain
            columns ``ticker``, ``date``, ``momentum_score``.
        volume_weighted_momentum_scores: Output of
            compute_volume_weighted_momentum_21d_scores(). Must contain
            columns ``ticker``, ``date``, ``volume_weighted_momentum_21d_score``.
        obv_momentum_scores: Output of compute_obv_momentum_21d_scores(). Must
            contain columns ``ticker``, ``date``, ``obv_momentum_21d_score``.
        momentum_weight: Relative weight for price momentum. Default 0.5.
        volume_weighted_momentum_weight: Relative weight for volume-weighted
            momentum. Default 0.3.
        obv_momentum_weight: Relative weight for OBV momentum. Default 0.2.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``momentum_score``,
            ``volume_weighted_momentum_21d_score``, ``obv_momentum_21d_score``,
            ``volume_momentum_score``

        Rows present in only one or two inputs are retained with NaN for the
        missing dimension(s); their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(momentum_scores, "momentum_scores", ["momentum_score"])
    _validate(
        volume_weighted_momentum_scores,
        "volume_weighted_momentum_scores",
        ["volume_weighted_momentum_21d_score"],
    )
    _validate(obv_momentum_scores, "obv_momentum_scores", ["obv_momentum_21d_score"])

    merged = (
        momentum_scores[["ticker", "date", "momentum_score"]]
        .merge(
            volume_weighted_momentum_scores[
                ["ticker", "date", "volume_weighted_momentum_21d_score"]
            ],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            obv_momentum_scores[["ticker", "date", "obv_momentum_21d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "momentum_score": momentum_weight,
        "volume_weighted_momentum_21d_score": volume_weighted_momentum_weight,
        "obv_momentum_21d_score": obv_momentum_weight,
    }
    result = blend_scores(merged, weights, "volume_momentum_score")
    result = result.dropna(subset=["volume_momentum_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "volume_momentum_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        momentum_weight=momentum_weight,
        volume_weighted_momentum_weight=volume_weighted_momentum_weight,
        obv_momentum_weight=obv_momentum_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

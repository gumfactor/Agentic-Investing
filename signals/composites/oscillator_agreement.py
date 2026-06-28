"""Oscillator Agreement composite signal.

Blends three momentum oscillators that measure buying pressure from different
angles into a single directional confirmation score.

The thesis: individual oscillators generate frequent false signals. When RSI
(recent buying pressure magnitude), the MACD histogram (momentum acceleration),
and the Stochastic %K (price position within its recent range) all agree on
direction cross-sectionally, the combined signal has a substantially lower
false-positive rate than any single oscillator.

Important framing: all three input scores are *cross-sectional* z-scores, not
absolute oscillator levels. "Higher" means "more bullish than peers today,"
not "overbought in absolute terms." This composite is appropriate for
momentum-style strategies. For contrarian / mean-reversion strategies that
need absolute overbought/oversold thresholds, use raw-value oscillator
variants (to be added in Group 4).

Basis: Appel "Technical Analysis: Power Tools for Active Investors" (2005);
Wilder "New Concepts in Technical Trading Systems" (1978); practitioner
literature on oscillator confluence / multi-indicator confirmation.

Default weighting: RSI 40%, MACD histogram 35%, Stochastic %K 25%.
RSI is the most widely followed and empirically robust; MACD histogram
captures acceleration (the second derivative of price); Stochastic %K is
shorter-term and given slightly less weight as a result.

Inputs
------
rsi_scores: Output of compute_rsi_14_scores(). Must contain
    ``ticker``, ``date``, ``rsi_14_score``.
macd_histogram_scores: Output of compute_macd_histogram_12_26_9_scores(). Must
    contain ``ticker``, ``date``, ``macd_histogram_12_26_9_score``.
stoch_k_scores: Output of compute_stoch_k_14_scores(). Must contain
    ``ticker``, ``date``, ``stoch_k_14_score``.

Output
------
oscillator_agreement_score: weighted blend, cross-sectionally re-standardized
per date. Higher = multiple oscillators simultaneously indicating relative
bullish momentum.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_oscillator_agreement_scores(
    rsi_scores: pd.DataFrame,
    macd_histogram_scores: pd.DataFrame,
    stoch_k_scores: pd.DataFrame,
    rsi_weight: float = 0.4,
    macd_histogram_weight: float = 0.35,
    stoch_k_weight: float = 0.25,
) -> pd.DataFrame:
    """Blend RSI, MACD histogram, and Stochastic %K into an oscillator-agreement composite.

    Args:
        rsi_scores: Output of compute_rsi_14_scores(). Must contain columns
            ``ticker``, ``date``, ``rsi_14_score``.
        macd_histogram_scores: Output of compute_macd_histogram_12_26_9_scores().
            Must contain columns ``ticker``, ``date``,
            ``macd_histogram_12_26_9_score``.
        stoch_k_scores: Output of compute_stoch_k_14_scores(). Must contain
            columns ``ticker``, ``date``, ``stoch_k_14_score``.
        rsi_weight: Relative weight for RSI. Default 0.4.
        macd_histogram_weight: Relative weight for MACD histogram. Default 0.35.
        stoch_k_weight: Relative weight for Stochastic %K. Default 0.25.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``rsi_14_score``,
            ``macd_histogram_12_26_9_score``, ``stoch_k_14_score``,
            ``oscillator_agreement_score``

        Rows present in only one or two inputs are retained with NaN for the
        missing dimension(s); their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(rsi_scores, "rsi_scores", ["rsi_14_score"])
    _validate(macd_histogram_scores, "macd_histogram_scores", ["macd_histogram_12_26_9_score"])
    _validate(stoch_k_scores, "stoch_k_scores", ["stoch_k_14_score"])

    merged = (
        rsi_scores[["ticker", "date", "rsi_14_score"]]
        .merge(
            macd_histogram_scores[["ticker", "date", "macd_histogram_12_26_9_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            stoch_k_scores[["ticker", "date", "stoch_k_14_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "rsi_14_score": rsi_weight,
        "macd_histogram_12_26_9_score": macd_histogram_weight,
        "stoch_k_14_score": stoch_k_weight,
    }
    result = blend_scores(merged, weights, "oscillator_agreement_score")
    result = result.dropna(subset=["oscillator_agreement_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "oscillator_agreement_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        rsi_weight=rsi_weight,
        macd_histogram_weight=macd_histogram_weight,
        stoch_k_weight=stoch_k_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

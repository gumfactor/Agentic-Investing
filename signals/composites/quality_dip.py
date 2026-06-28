"""Quality Dip composite signal.

Identifies high-quality companies that are temporarily technically oversold —
a superior entry point compared to either quality alone (expensive) or
oversold alone (potentially distressed).

The thesis: the biggest risk in contrarian investing is buying a "value trap" —
a cheap stock that is cheap for fundamental reasons and stays cheap. The Quality
Dip composite mitigates this by requiring strong fundamental quality before
acting on a technical oversold signal. A high-quality firm that has sold off
due to market sentiment, sector rotation, or short-term earnings noise (not
structural impairment) is likely to revert to fair value faster and with less
permanent capital loss than a low-quality oversold stock.

The price-depressed signals are two independent measures:
  (a) RSI(14) raw: below-average RSI in absolute terms signals short-term
      selling exhaustion regardless of what peers are doing.
  (b) Rolling z-score vs own 252-day history: how many standard deviations
      the stock is below its own annual average, independent of cross-
      sectional peer comparison.

Both oscillator signals are negated internally (lower raw value = more
oversold = higher composite score contribution). The quality dimension uses
the pre-computed composite quality_score, which is already cross-sectionally
z-scored and higher = better.

Basis: Quantitative quality-at-a-dip strategies; practitioner overlap between
the Piotroski F-score and RSI mean-reversion screens; Lakonishok, Shleifer &
Vishny (1994) on contrarian value strategies.

Default weighting: quality_score 50%, rsi_oversold 30%, price_depressed 20%.
Quality carries the most weight because it determines whether the dip is a
buying opportunity or a value trap.

Inputs
------
quality_scores: Output of compute_quality_scores(). Must contain
    ``ticker``, ``date``, ``quality_score``.
rsi_raw_scores: Output of compute_rsi_14_raw_scores(). Must contain
    ``ticker``, ``date``, ``rsi_14_raw``.
rolling_zscore_raw_scores: Output of compute_rolling_zscore_252d_raw_scores().
    Must contain ``ticker``, ``date``, ``rolling_zscore_252d_raw``.

Output
------
quality_dip_score: weighted blend, cross-sectionally re-standardized per date.
Higher = high-quality company that is oversold both on short-term RSI and
relative to its own annual price history.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def _cs_normalize(s: pd.Series) -> pd.Series:
    """Cross-sectional z-score per date group; returns zeros for tied values, preserves NaN."""
    std = s.std(ddof=1)
    if pd.isna(std) or std == 0:
        return s * 0.0
    return (s - s.mean()) / std


def compute_quality_dip_scores(
    quality_scores: pd.DataFrame,
    rsi_raw_scores: pd.DataFrame,
    rolling_zscore_raw_scores: pd.DataFrame,
    quality_weight: float = 0.50,
    rsi_weight: float = 0.30,
    rolling_zscore_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend quality with inverted RSI and 252d price z-score into a quality-dip composite.

    Args:
        quality_scores: Output of compute_quality_scores(). Must contain
            columns ``ticker``, ``date``, ``quality_score``.
        rsi_raw_scores: Output of compute_rsi_14_raw_scores(). Must contain
            columns ``ticker``, ``date``, ``rsi_14_raw``.
        rolling_zscore_raw_scores: Output of
            compute_rolling_zscore_252d_raw_scores(). Must contain columns
            ``ticker``, ``date``, ``rolling_zscore_252d_raw``.
        quality_weight: Relative weight for quality. Default 0.50.
        rsi_weight: Relative weight for RSI oversold signal. Default 0.30.
        rolling_zscore_weight: Relative weight for price-depressed signal.
            Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``quality_score``, ``rsi_14_raw``,
            ``rolling_zscore_252d_raw``, ``quality_dip_score``

        The RSI and rolling z-score values are negated internally before
        blending so that lower raw values (more oversold) contribute a
        higher score. The original raw values are preserved in the output
        for transparency.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(quality_scores, "quality_scores", ["quality_score"])
    _validate(rsi_raw_scores, "rsi_raw_scores", ["rsi_14_raw"])
    _validate(rolling_zscore_raw_scores, "rolling_zscore_raw_scores", ["rolling_zscore_252d_raw"])

    merged = (
        quality_scores[["ticker", "date", "quality_score"]]
        .merge(
            rsi_raw_scores[["ticker", "date", "rsi_14_raw"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            rolling_zscore_raw_scores[["ticker", "date", "rolling_zscore_252d_raw"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    # Negate raw oscillator values: lower RSI / lower price z-score = more oversold = higher score
    merged = merged.copy()
    merged["_rsi_oversold"] = -merged["rsi_14_raw"]
    merged["_price_depressed"] = -merged["rolling_zscore_252d_raw"]

    # Cross-sectionally normalize raw oscillators per date so RSI's 0–100 scale
    # does not overwhelm the unit-normal quality_score in the weighted blend.
    # Without this step, RSI variance (~20× larger) renders quality_weight ineffective.
    for _col in ("_rsi_oversold", "_price_depressed"):
        merged[_col] = merged.groupby("date")[_col].transform(_cs_normalize)

    weights = {
        "quality_score": quality_weight,
        "_rsi_oversold": rsi_weight,
        "_price_depressed": rolling_zscore_weight,
    }
    result = blend_scores(merged, weights, "quality_dip_score")

    # Drop internal negated columns; retain original raw values for transparency
    result = result.drop(columns=["_rsi_oversold", "_price_depressed"])
    result = result.dropna(subset=["quality_dip_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "quality_dip_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        quality_weight=quality_weight,
        rsi_weight=rsi_weight,
        rolling_zscore_weight=rolling_zscore_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

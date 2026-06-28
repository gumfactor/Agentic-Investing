"""Defensive Quality composite signal.

Combines fundamental quality with low-market-sensitivity characteristics to
produce a portfolio that holds up in both normal and risk-off environments.

The thesis: high-quality businesses are more predictable and resilient, but
quality alone can concentrate in high-beta sectors (technology, consumer
discretionary) that amplify drawdowns in downturns. Overlaying a low-beta
filter and a positive-skew return screen selects quality companies with
genuinely defensive price behaviour. Asness et al. (2019) document the
"quality minus junk" premium; Clarke et al. (2006) show minimum-variance
portfolios substantially outperform on a risk-adjusted basis.

The three signals are mutually reinforcing:
  (a) quality_score: composite fundamental quality — profitability, leverage,
      earnings quality. Higher = better business fundamentals.
  (b) beta_252d: 252-day OLS beta vs SPY, negated — lower beta means the
      stock responds less to broad market moves, reducing systematic drawdowns.
  (c) up_down_vol_ratio_63d: upside deviation / downside deviation over 63
      days. Values above 1 signal positive return asymmetry — gains tend to
      be larger than losses — which is the hallmark of a defensive compounder.

The beta signal is negated internally (lower raw beta = more defensive =
higher composite score contribution). Raw values are preserved in the output
for transparency.

Default weighting: quality 50%, low-beta 25%, up/down vol ratio 25%.
Quality is the primary fundamental anchor; the two defensive signals provide
the risk overlay.

Inputs
------
quality_scores: Output of compute_quality_scores(). Must contain ``ticker``,
    ``date``, ``quality_score``.
beta_scores: Output of compute_beta_252d_scores(). Must contain ``ticker``,
    ``date``, ``beta_252d_score``.
up_down_vol_scores: Output of compute_up_down_vol_ratio_63d_scores(). Must
    contain ``ticker``, ``date``, ``up_down_vol_ratio_63d_score``.

Output
------
defensive_quality_score: weighted blend, cross-sectionally re-standardized
per date. Higher = high-quality business + low market beta + positive return
asymmetry (gains > losses).
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_defensive_quality_scores(
    quality_scores: pd.DataFrame,
    beta_scores: pd.DataFrame,
    up_down_vol_scores: pd.DataFrame,
    quality_weight: float = 0.50,
    beta_weight: float = 0.25,
    up_down_vol_weight: float = 0.25,
) -> pd.DataFrame:
    """Blend quality with inverted beta and up/down volatility ratio.

    Args:
        quality_scores: Output of compute_quality_scores(). Must contain
            columns ``ticker``, ``date``, ``quality_score``.
        beta_scores: Output of compute_beta_252d_scores(). Must contain
            columns ``ticker``, ``date``, ``beta_252d_score``.
        up_down_vol_scores: Output of compute_up_down_vol_ratio_63d_scores().
            Must contain columns ``ticker``, ``date``,
            ``up_down_vol_ratio_63d_score``.
        quality_weight: Relative weight for quality. Default 0.50.
        beta_weight: Relative weight for the low-beta signal (inverted beta).
            Default 0.25.
        up_down_vol_weight: Relative weight for up/down volatility ratio.
            Default 0.25.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``quality_score``, ``beta_252d_score``,
            ``up_down_vol_ratio_63d_score``, ``defensive_quality_score``

        ``beta_252d_score`` is negated internally so that lower beta
        contributes a higher composite score. The original raw value is
        preserved in the output column.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(quality_scores, "quality_scores", ["quality_score"])
    _validate(beta_scores, "beta_scores", ["beta_252d_score"])
    _validate(up_down_vol_scores, "up_down_vol_scores", ["up_down_vol_ratio_63d_score"])

    merged = (
        quality_scores[["ticker", "date", "quality_score"]]
        .merge(
            beta_scores[["ticker", "date", "beta_252d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            up_down_vol_scores[["ticker", "date", "up_down_vol_ratio_63d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    # Negate beta: lower market sensitivity = more defensive = higher score
    merged = merged.copy()
    merged["_low_beta"] = -merged["beta_252d_score"]

    weights = {
        "quality_score": quality_weight,
        "_low_beta": beta_weight,
        "up_down_vol_ratio_63d_score": up_down_vol_weight,
    }
    result = blend_scores(merged, weights, "defensive_quality_score")

    result = result.drop(columns=["_low_beta"])
    result = result.dropna(subset=["defensive_quality_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "defensive_quality_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        quality_weight=quality_weight,
        beta_weight=beta_weight,
        up_down_vol_weight=up_down_vol_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

"""Growth Score composite signal.

Aggregates the most reliable and distinct growth dimensions into a single
cross-sectionally comparable score. This is the foundational "growth factor"
composite — the growth-category parallel to quality_score and value_score —
and is designed to be consumed directly by strategies or fed into higher-level
composites (sustainable_growth, growth_momentum, GARP variants).

The five dimensions cover the full income-statement and cash-flow trajectory:
  (a) revenue_growth_3y_cagr: compound top-line growth over three years.
      Revenue is almost always positive, making the CAGR reliable. The
      3-year window smooths project-timing and seasonal noise while
      remaining actionable.
  (b) eps_growth_3y_cagr: compound earnings growth over three years.
      Captures whether profits are compounding. Three-year smoothing reduces
      the influence of single-year write-downs and tax effects.
  (c) fcf_growth_3y_cagr: compound free cash flow growth over three years.
      FCF is hardest to manipulate and most directly linked to intrinsic
      value. Only defined when base-year FCF is positive, so it naturally
      excludes cash-burning companies.
  (d) operating_margin_expansion_yoy: year-on-year change in operating
      margin. Revenue growth without margin expansion signals a business
      competing on price rather than differentiation; margin expansion
      confirms quality-of-growth.
  (e) eps_growth_acceleration: second derivative of earnings — whether the
      growth rate itself is accelerating or decelerating. Catches inflection
      points before the market re-rates the stock.

Basis: Fama-French growth factor literature; O'Neil "How to Make Money in
Stocks" on earnings acceleration; Novy-Marx on fundamental momentum;
empirical evidence that multi-period revenue growth predicts returns.

Default weighting: revenue CAGR 25%, EPS CAGR 25%, FCF CAGR 20%, margin
expansion 15%, EPS acceleration 15%. Revenue and EPS CAGRs anchor the
score; FCF provides a cash-quality check; margin expansion and acceleration
capture the direction of the growth trajectory.

Inputs
------
revenue_growth_scores: Output of compute_revenue_growth_3y_cagr_scores().
    Must contain ``ticker``, ``date``, ``revenue_growth_3y_cagr_score``.
eps_growth_scores: Output of compute_eps_growth_3y_cagr_scores(). Must
    contain ``ticker``, ``date``, ``eps_growth_3y_cagr_score``.
fcf_growth_scores: Output of compute_fcf_growth_3y_cagr_scores(). Must
    contain ``ticker``, ``date``, ``fcf_growth_3y_cagr_score``.
margin_expansion_scores: Output of compute_operating_margin_expansion_yoy_scores().
    Must contain ``ticker``, ``date``,
    ``operating_margin_expansion_yoy_score``.
eps_acceleration_scores: Output of compute_eps_growth_acceleration_scores().
    Must contain ``ticker``, ``date``, ``eps_growth_acceleration_score``.

Output
------
growth_score: weighted blend, cross-sectionally re-standardized per date.
Higher = faster durable revenue and earnings compounding + expanding
operating margins + accelerating earnings growth.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_growth_scores(
    revenue_growth_scores: pd.DataFrame,
    eps_growth_scores: pd.DataFrame,
    fcf_growth_scores: pd.DataFrame,
    margin_expansion_scores: pd.DataFrame,
    eps_acceleration_scores: pd.DataFrame,
    revenue_growth_weight: float = 0.25,
    eps_growth_weight: float = 0.25,
    fcf_growth_weight: float = 0.20,
    margin_expansion_weight: float = 0.15,
    eps_acceleration_weight: float = 0.15,
) -> pd.DataFrame:
    """Blend five growth dimensions into the foundational growth_score composite.

    Args:
        revenue_growth_scores: Output of compute_revenue_growth_3y_cagr_scores().
            Must contain columns ``ticker``, ``date``,
            ``revenue_growth_3y_cagr_score``.
        eps_growth_scores: Output of compute_eps_growth_3y_cagr_scores().
            Must contain columns ``ticker``, ``date``,
            ``eps_growth_3y_cagr_score``.
        fcf_growth_scores: Output of compute_fcf_growth_3y_cagr_scores().
            Must contain columns ``ticker``, ``date``,
            ``fcf_growth_3y_cagr_score``.
        margin_expansion_scores: Output of
            compute_operating_margin_expansion_yoy_scores(). Must contain
            columns ``ticker``, ``date``,
            ``operating_margin_expansion_yoy_score``.
        eps_acceleration_scores: Output of
            compute_eps_growth_acceleration_scores(). Must contain columns
            ``ticker``, ``date``, ``eps_growth_acceleration_score``.
        revenue_growth_weight: Relative weight for revenue CAGR. Default 0.25.
        eps_growth_weight: Relative weight for EPS CAGR. Default 0.25.
        fcf_growth_weight: Relative weight for FCF CAGR. Default 0.20.
        margin_expansion_weight: Relative weight for operating margin
            expansion. Default 0.15.
        eps_acceleration_weight: Relative weight for EPS growth acceleration.
            Default 0.15.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``revenue_growth_3y_cagr_score``,
            ``eps_growth_3y_cagr_score``, ``fcf_growth_3y_cagr_score``,
            ``operating_margin_expansion_yoy_score``,
            ``eps_growth_acceleration_score``, ``growth_score``

        All five input signals point in the same direction (higher = better
        growth); no internal negation is required.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(revenue_growth_scores, "revenue_growth_scores", ["revenue_growth_3y_cagr_score"])
    _validate(eps_growth_scores, "eps_growth_scores", ["eps_growth_3y_cagr_score"])
    _validate(fcf_growth_scores, "fcf_growth_scores", ["fcf_growth_3y_cagr_score"])
    _validate(margin_expansion_scores, "margin_expansion_scores", ["operating_margin_expansion_yoy_score"])
    _validate(eps_acceleration_scores, "eps_acceleration_scores", ["eps_growth_acceleration_score"])

    merged = (
        revenue_growth_scores[["ticker", "date", "revenue_growth_3y_cagr_score"]]
        .merge(
            eps_growth_scores[["ticker", "date", "eps_growth_3y_cagr_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            fcf_growth_scores[["ticker", "date", "fcf_growth_3y_cagr_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            margin_expansion_scores[["ticker", "date", "operating_margin_expansion_yoy_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            eps_acceleration_scores[["ticker", "date", "eps_growth_acceleration_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "revenue_growth_3y_cagr_score": revenue_growth_weight,
        "eps_growth_3y_cagr_score": eps_growth_weight,
        "fcf_growth_3y_cagr_score": fcf_growth_weight,
        "operating_margin_expansion_yoy_score": margin_expansion_weight,
        "eps_growth_acceleration_score": eps_acceleration_weight,
    }
    result = blend_scores(merged, weights, "growth_score")

    result = result.dropna(subset=["growth_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "growth_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        revenue_growth_weight=revenue_growth_weight,
        eps_growth_weight=eps_growth_weight,
        fcf_growth_weight=fcf_growth_weight,
        margin_expansion_weight=margin_expansion_weight,
        eps_acceleration_weight=eps_acceleration_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

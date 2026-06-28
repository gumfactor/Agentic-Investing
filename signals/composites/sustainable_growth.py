"""Sustainable Growth composite signal.

Combines fundamental growth with quality and capital-efficiency filters to
identify genuine compounders — businesses that can grow without deteriorating
their return profile.

The thesis: raw growth is not uniformly valuable. A business can grow revenue
quickly while destroying shareholder value if growth is funded by dilutive
equity, levered acquisitions, or commoditised price competition. Sustainable
growth requires three conditions simultaneously: (1) the business is actually
growing (growth_score), (2) it is high-quality on a cross-sectional basis
(quality_score), and (3) it is deploying capital more productively each year
(ROIC improvement). This is the quantitative analog of "wonderful businesses
at fair prices" (Buffett) and the "franchise quality" literature.

The three signals are complementary with different failure modes:
  (a) growth_score: composite growth across revenue, EPS, FCF, margin
      expansion, and EPS acceleration. Primary growth signal.
  (b) quality_score: composite quality across profitability, leverage, and
      earnings quality. Filters out highly-levered or low-margin growers.
  (c) roic_improvement_yoy: year-on-year change in ROIC. Distinguishes
      value-creating growth (rising ROIC above cost of capital) from
      value-destroying growth (ROIC declining despite volume expansion).

Basis: Buffett/Munger "wonderful businesses at fair prices"; Novy-Marx
(2013) on the profitability premium; Mauboussin on ROIC as the primary
driver of long-run value creation; empirical evidence that quality x growth
combinations reduce value-trap and value-destruction exposure.

Default weighting: growth_score 50%, quality_score 30%, ROIC improvement
20%. Growth is the primary thesis; quality and ROIC improvement serve as
sustainability filters rather than co-equal signals.

Inputs
------
growth_scores: Output of compute_growth_scores(). Must contain ``ticker``,
    ``date``, ``growth_score``.
quality_scores: Output of compute_quality_scores(). Must contain ``ticker``,
    ``date``, ``quality_score``.
roic_improvement_scores: Output of compute_roic_improvement_yoy_scores().
    Must contain ``ticker``, ``date``, ``roic_improvement_yoy_score``.

Output
------
sustainable_growth_score: weighted blend, cross-sectionally re-standardized
per date. Higher = fast durable growth + high quality + improving capital
efficiency (rising ROIC).
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_sustainable_growth_scores(
    growth_scores: pd.DataFrame,
    quality_scores: pd.DataFrame,
    roic_improvement_scores: pd.DataFrame,
    growth_weight: float = 0.50,
    quality_weight: float = 0.30,
    roic_improvement_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend growth with quality and ROIC-improvement filters.

    Args:
        growth_scores: Output of compute_growth_scores(). Must contain
            columns ``ticker``, ``date``, ``growth_score``.
        quality_scores: Output of compute_quality_scores(). Must contain
            columns ``ticker``, ``date``, ``quality_score``.
        roic_improvement_scores: Output of
            compute_roic_improvement_yoy_scores(). Must contain columns
            ``ticker``, ``date``, ``roic_improvement_yoy_score``.
        growth_weight: Relative weight for growth. Default 0.50.
        quality_weight: Relative weight for quality. Default 0.30.
        roic_improvement_weight: Relative weight for ROIC improvement.
            Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``growth_score``, ``quality_score``,
            ``roic_improvement_yoy_score``, ``sustainable_growth_score``

        All three signals point higher = better; no internal negation is
        required.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(growth_scores, "growth_scores", ["growth_score"])
    _validate(quality_scores, "quality_scores", ["quality_score"])
    _validate(roic_improvement_scores, "roic_improvement_scores", ["roic_improvement_yoy_score"])

    merged = (
        growth_scores[["ticker", "date", "growth_score"]]
        .merge(
            quality_scores[["ticker", "date", "quality_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            roic_improvement_scores[["ticker", "date", "roic_improvement_yoy_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "growth_score": growth_weight,
        "quality_score": quality_weight,
        "roic_improvement_yoy_score": roic_improvement_weight,
    }
    result = blend_scores(merged, weights, "sustainable_growth_score")

    result = result.dropna(subset=["sustainable_growth_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "sustainable_growth_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        growth_weight=growth_weight,
        quality_weight=quality_weight,
        roic_improvement_weight=roic_improvement_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

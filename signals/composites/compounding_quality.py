"""Compounding Quality composite signal.

Identifies companies that earn high returns on the capital they deploy and
sustain durable margins — the hallmarks of a wide economic moat.

The thesis: businesses that consistently earn ROIC well above their cost of
capital create economic value with every dollar of reinvestment. Over time,
this compounds into growing intrinsic value per share. High gross margins
signal pricing power (customers value the product enough to pay a premium);
high operating margins confirm that pricing power survives the cost structure.
Together, ROIC, ROCE, gross margin, and operating margin triangulate the
quality of a business franchise from multiple angles.

Basis: Novy-Marx "The Other Side of Value" (2013) on gross profitability;
Greenblatt "The Little Book That Beats the Market" (2005) on ROIC as the
primary quality signal; Buffett's emphasis on businesses that can reinvest
retained earnings at high rates; Koller, Goedhart & Wessels "Valuation" on
ROIC as the single most important driver of value creation.

Default weighting: roic 40%, roce 25%, gross_margin 20%, operating_margin 15%.
ROIC carries the most weight as it is the most comprehensive measure of
capital efficiency. ROCE provides a structural cross-check using a different
(but related) capital denominator. Margin signals are secondary evidence that
quality flows through to the income statement.

Inputs
------
roic_scores: Output of compute_roic_scores(). Must contain
    ``ticker``, ``date``, ``roic_score``.
roce_scores: Output of compute_roce_scores(). Must contain
    ``ticker``, ``date``, ``roce_score``.
gross_margin_scores: Output of compute_gross_margin_scores(). Must contain
    ``ticker``, ``date``, ``gross_margin_score``.
operating_margin_scores: Output of compute_operating_margin_scores(). Must
    contain ``ticker``, ``date``, ``operating_margin_score``.

Output
------
compounding_quality_score: weighted blend, cross-sectionally re-standardized
per date. Higher = durable economic moat with strong capital returns and
sustainable margins.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_compounding_quality_scores(
    roic_scores: pd.DataFrame,
    roce_scores: pd.DataFrame,
    gross_margin_scores: pd.DataFrame,
    operating_margin_scores: pd.DataFrame,
    roic_weight: float = 0.40,
    roce_weight: float = 0.25,
    gross_margin_weight: float = 0.20,
    operating_margin_weight: float = 0.15,
) -> pd.DataFrame:
    """Blend ROIC, ROCE, and margin indicators into a compounding-quality composite.

    Args:
        roic_scores: Output of compute_roic_scores(). Must contain columns
            ``ticker``, ``date``, ``roic_score``.
        roce_scores: Output of compute_roce_scores(). Must contain columns
            ``ticker``, ``date``, ``roce_score``.
        gross_margin_scores: Output of compute_gross_margin_scores(). Must
            contain columns ``ticker``, ``date``, ``gross_margin_score``.
        operating_margin_scores: Output of compute_operating_margin_scores().
            Must contain columns ``ticker``, ``date``,
            ``operating_margin_score``.
        roic_weight: Relative weight for ROIC. Default 0.40.
        roce_weight: Relative weight for ROCE. Default 0.25.
        gross_margin_weight: Relative weight for gross margin. Default 0.20.
        operating_margin_weight: Relative weight for operating margin.
            Default 0.15.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``roic_score``, ``roce_score``,
            ``gross_margin_score``, ``operating_margin_score``,
            ``compounding_quality_score``

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(roic_scores, "roic_scores", ["roic_score"])
    _validate(roce_scores, "roce_scores", ["roce_score"])
    _validate(gross_margin_scores, "gross_margin_scores", ["gross_margin_score"])
    _validate(operating_margin_scores, "operating_margin_scores", ["operating_margin_score"])

    merged = (
        roic_scores[["ticker", "date", "roic_score"]]
        .merge(
            roce_scores[["ticker", "date", "roce_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            gross_margin_scores[["ticker", "date", "gross_margin_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            operating_margin_scores[["ticker", "date", "operating_margin_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "roic_score": roic_weight,
        "roce_score": roce_weight,
        "gross_margin_score": gross_margin_weight,
        "operating_margin_score": operating_margin_weight,
    }
    result = blend_scores(merged, weights, "compounding_quality_score")
    result = result.dropna(subset=["compounding_quality_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "compounding_quality_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        roic_weight=roic_weight,
        roce_weight=roce_weight,
        gross_margin_weight=gross_margin_weight,
        operating_margin_weight=operating_margin_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

"""Growth at a Reasonable Price (GARP) composite signal.

Combines earnings cheapness (earnings yield) with earnings growth durability
(3-year EPS CAGR) and optionally revenue growth (3-year revenue CAGR).

The thesis: paying less for faster, more durable growth is the core of GARP.
A stock with high earnings yield AND high earnings growth outperforms because:
  (a) cheapness provides a margin of safety, and
  (b) growth compounds forward returns.

Classic formulation: PEG ratio = P/E ÷ EPS_growth. Its inverse (earnings_yield
× EPS_growth) is what this composite maximises. Revenue growth is added as a
top-line validation — earnings that are not supported by revenue growth are more
likely to be unsustainable.

Basis: Peter Lynch "One Up On Wall Street" (1989); Damodaran PEG analysis;
widely used in systematic GARP equity strategies.

Inputs
------
value_scores: Output of compute_value_scores(). Provides ``earnings_yield``
    (already z-scored), the cheapness leg of GARP.
eps_growth_scores: Output of compute_eps_growth_3y_cagr_scores(). Provides
    ``eps_growth_3y_cagr_score`` (already z-scored).
revenue_growth_scores: Optional output of compute_revenue_growth_3y_cagr_scores().
    Provides ``revenue_growth_3y_cagr_score``. When omitted, its weight is
    redistributed to the other two signals via per-row renormalization.

Output
------
garp_score: weighted blend, cross-sectionally re-standardized per date.
Higher = cheaper + faster-growing + (optionally) stronger top-line growth.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_garp_scores(
    value_scores: pd.DataFrame,
    eps_growth_scores: pd.DataFrame,
    revenue_growth_scores: Optional[pd.DataFrame] = None,
    earnings_yield_weight: float = 0.4,
    eps_growth_weight: float = 0.4,
    revenue_growth_weight: float = 0.2,
) -> pd.DataFrame:
    """Compute GARP (Growth at a Reasonable Price) composite scores.

    Args:
        value_scores: Output of compute_value_scores(). Must contain columns
            ``ticker``, ``date``, ``earnings_yield``.
        eps_growth_scores: Output of compute_eps_growth_3y_cagr_scores(). Must
            contain columns ``ticker``, ``date``, ``eps_growth_3y_cagr_score``.
        revenue_growth_scores: Optional output of
            compute_revenue_growth_3y_cagr_scores(). When None, its weight is
            absorbed by the other two signals via per-row renormalization.
        earnings_yield_weight: Default 0.4.
        eps_growth_weight: Default 0.4.
        revenue_growth_weight: Default 0.2. Ignored when revenue_growth_scores
            is None (redistributed automatically).

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``,
            ``earnings_yield``, ``eps_growth_3y_cagr_score``,
            ``revenue_growth_3y_cagr_score`` (if supplied),
            ``garp_score``
    """
    _validate(value_scores, "value_scores", ["earnings_yield"])
    _validate(eps_growth_scores, "eps_growth_scores", ["eps_growth_3y_cagr_score"])

    merged = (
        value_scores[["ticker", "date", "earnings_yield"]]
        .merge(
            eps_growth_scores[["ticker", "date", "eps_growth_3y_cagr_score"]],
            on=["ticker", "date"],
            how="outer",
        )
    )

    weights: dict[str, float] = {
        "earnings_yield": earnings_yield_weight,
        "eps_growth_3y_cagr_score": eps_growth_weight,
    }

    if revenue_growth_scores is not None:
        _validate(revenue_growth_scores, "revenue_growth_scores", ["revenue_growth_3y_cagr_score"])
        merged = merged.merge(
            revenue_growth_scores[["ticker", "date", "revenue_growth_3y_cagr_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        weights["revenue_growth_3y_cagr_score"] = revenue_growth_weight

    merged = merged.reset_index(drop=True)
    result = blend_scores(merged, weights, "garp_score")
    result = result.dropna(subset=["garp_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "garp_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        include_revenue_growth=revenue_growth_scores is not None,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

"""Earnings Conviction composite signal.

Measures whether reported earnings are real, persistent, and predictable —
three independent dimensions of earnings quality.

The thesis: not all earnings are equal. Earnings backed by operating cash
flow (low accruals, high cash conversion ratio) are more likely to recur.
Earnings that are consistent across quarters and stable over time are more
predictable and less likely to disappoint. Companies that score well on all
four dimensions are high-conviction earners: their income statements reflect
genuine economic activity rather than accounting discretion.

The Sloan (1996) accrual anomaly is one of the most replicated findings in
academic finance: low-accrual firms persistently outperform high-accrual
firms because the market overestimates the persistence of accrual-based
earnings. Cash earnings ratio provides a second, independent cash-backing
signal. Earnings consistency and EPS stability capture the durability and
predictability dimensions respectively.

Basis: Sloan "Do Stock Prices Fully Reflect Information in Accruals and Cash
Flows about Future Earnings?" (1996); Richardson et al. "Accruals, Cash Flows,
and Equity Values" (2010); Dechow & Schrand "Earnings Quality" (2004).

Default weighting: sloan_accrual 30%, cash_earnings_ratio 30%,
earnings_consistency 25%, eps_stability 15%. Both accrual signals share
equal primary weight as they measure the same dimension (cash backing)
via complementary methods. Consistency and stability are secondary because
they measure durability rather than the accrual anomaly directly.

Note: sloan_accrual_score and eps_stability_score are already negated at
the indicator level — higher scores already mean better earnings quality.

Inputs
------
sloan_accrual_scores: Output of compute_sloan_accrual_scores(). Must contain
    ``ticker``, ``date``, ``sloan_accrual_score``.
cash_earnings_ratio_scores: Output of compute_cash_earnings_ratio_scores().
    Must contain ``ticker``, ``date``, ``cash_earnings_ratio_score``.
earnings_consistency_scores: Output of compute_earnings_consistency_scores().
    Must contain ``ticker``, ``date``, ``earnings_consistency_score``.
eps_stability_scores: Output of compute_eps_stability_scores(). Must contain
    ``ticker``, ``date``, ``eps_stability_score``.

Output
------
earnings_conviction_score: weighted blend, cross-sectionally re-standardized
per date. Higher = cash-backed, consistent, stable earnings with low accrual
content.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_earnings_conviction_scores(
    sloan_accrual_scores: pd.DataFrame,
    cash_earnings_ratio_scores: pd.DataFrame,
    earnings_consistency_scores: pd.DataFrame,
    eps_stability_scores: pd.DataFrame,
    sloan_accrual_weight: float = 0.30,
    cash_earnings_ratio_weight: float = 0.30,
    earnings_consistency_weight: float = 0.25,
    eps_stability_weight: float = 0.15,
) -> pd.DataFrame:
    """Blend accrual, cash-backing, consistency, and stability into an earnings-quality composite.

    Args:
        sloan_accrual_scores: Output of compute_sloan_accrual_scores(). Must
            contain columns ``ticker``, ``date``, ``sloan_accrual_score``.
        cash_earnings_ratio_scores: Output of
            compute_cash_earnings_ratio_scores(). Must contain columns
            ``ticker``, ``date``, ``cash_earnings_ratio_score``.
        earnings_consistency_scores: Output of
            compute_earnings_consistency_scores(). Must contain columns
            ``ticker``, ``date``, ``earnings_consistency_score``.
        eps_stability_scores: Output of compute_eps_stability_scores(). Must
            contain columns ``ticker``, ``date``, ``eps_stability_score``.
        sloan_accrual_weight: Relative weight. Default 0.30.
        cash_earnings_ratio_weight: Relative weight. Default 0.30.
        earnings_consistency_weight: Relative weight. Default 0.25.
        eps_stability_weight: Relative weight. Default 0.15.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``sloan_accrual_score``,
            ``cash_earnings_ratio_score``, ``earnings_consistency_score``,
            ``eps_stability_score``, ``earnings_conviction_score``

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(sloan_accrual_scores, "sloan_accrual_scores", ["sloan_accrual_score"])
    _validate(cash_earnings_ratio_scores, "cash_earnings_ratio_scores", ["cash_earnings_ratio_score"])
    _validate(earnings_consistency_scores, "earnings_consistency_scores", ["earnings_consistency_score"])
    _validate(eps_stability_scores, "eps_stability_scores", ["eps_stability_score"])

    merged = (
        sloan_accrual_scores[["ticker", "date", "sloan_accrual_score"]]
        .merge(
            cash_earnings_ratio_scores[["ticker", "date", "cash_earnings_ratio_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            earnings_consistency_scores[["ticker", "date", "earnings_consistency_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            eps_stability_scores[["ticker", "date", "eps_stability_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "sloan_accrual_score": sloan_accrual_weight,
        "cash_earnings_ratio_score": cash_earnings_ratio_weight,
        "earnings_consistency_score": earnings_consistency_weight,
        "eps_stability_score": eps_stability_weight,
    }
    result = blend_scores(merged, weights, "earnings_conviction_score")
    result = result.dropna(subset=["earnings_conviction_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "earnings_conviction_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        sloan_accrual_weight=sloan_accrual_weight,
        cash_earnings_ratio_weight=cash_earnings_ratio_weight,
        earnings_consistency_weight=earnings_consistency_weight,
        eps_stability_weight=eps_stability_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

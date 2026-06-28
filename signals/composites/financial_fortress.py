"""Financial Fortress composite signal.

Combines four balance-sheet strength signals into a single measure of
financial resilience.

The thesis: companies with low net leverage, comfortable debt-service capacity,
and strong short-term liquidity can absorb economic shocks, avoid forced asset
sales, and take advantage of distressed opportunities that weaker balance sheets
cannot. This is the Graham-style safety margin applied systematically: a company
that scores well here is unlikely to face a liquidity crisis in the medium term.

In drawdown environments specifically, Financial Fortress scores predict lower
peak-to-trough loss because financially weak companies face both operational
stress and funding stress simultaneously.

Basis: Graham & Dodd "Security Analysis" (1934); Altman Z-score research on
distress prediction; Piotroski (2000) F-score leverage component; practitioner
use of net debt / EBITDA as the primary institutional credit screen.

Default weighting: net_debt_to_ebitda 35%, interest_coverage 30%,
current_ratio 20%, quick_ratio 15%. The two leverage/coverage signals carry
more weight because they are structural (multi-year obligations); liquidity
ratios are secondary point-in-time checks.

Note: all four input scores are already z-scored at the indicator level and
have been constructed so that higher = better (net_debt_to_ebitda and any
similar leverage indicators are negated before z-scoring).

Inputs
------
net_debt_ebitda_scores: Output of compute_net_debt_to_ebitda_scores(). Must
    contain ``ticker``, ``date``, ``net_debt_to_ebitda_score``.
interest_coverage_scores: Output of compute_interest_coverage_scores(). Must
    contain ``ticker``, ``date``, ``interest_coverage_score``.
current_ratio_scores: Output of compute_current_ratio_scores(). Must contain
    ``ticker``, ``date``, ``current_ratio_score``.
quick_ratio_scores: Output of compute_quick_ratio_scores(). Must contain
    ``ticker``, ``date``, ``quick_ratio_score``.

Output
------
financial_fortress_score: weighted blend, cross-sectionally re-standardized
per date. Higher = stronger balance sheet, lower financial risk.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_financial_fortress_scores(
    net_debt_ebitda_scores: pd.DataFrame,
    interest_coverage_scores: pd.DataFrame,
    current_ratio_scores: pd.DataFrame,
    quick_ratio_scores: pd.DataFrame,
    net_debt_ebitda_weight: float = 0.35,
    interest_coverage_weight: float = 0.30,
    current_ratio_weight: float = 0.20,
    quick_ratio_weight: float = 0.15,
) -> pd.DataFrame:
    """Blend leverage and liquidity indicators into a balance-sheet strength composite.

    Args:
        net_debt_ebitda_scores: Output of compute_net_debt_to_ebitda_scores().
            Must contain columns ``ticker``, ``date``,
            ``net_debt_to_ebitda_score``.
        interest_coverage_scores: Output of compute_interest_coverage_scores().
            Must contain columns ``ticker``, ``date``,
            ``interest_coverage_score``.
        current_ratio_scores: Output of compute_current_ratio_scores(). Must
            contain columns ``ticker``, ``date``, ``current_ratio_score``.
        quick_ratio_scores: Output of compute_quick_ratio_scores(). Must
            contain columns ``ticker``, ``date``, ``quick_ratio_score``.
        net_debt_ebitda_weight: Relative weight. Default 0.35.
        interest_coverage_weight: Relative weight. Default 0.30.
        current_ratio_weight: Relative weight. Default 0.20.
        quick_ratio_weight: Relative weight. Default 0.15.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``net_debt_to_ebitda_score``,
            ``interest_coverage_score``, ``current_ratio_score``,
            ``quick_ratio_score``, ``financial_fortress_score``

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(net_debt_ebitda_scores, "net_debt_ebitda_scores", ["net_debt_to_ebitda_score"])
    _validate(interest_coverage_scores, "interest_coverage_scores", ["interest_coverage_score"])
    _validate(current_ratio_scores, "current_ratio_scores", ["current_ratio_score"])
    _validate(quick_ratio_scores, "quick_ratio_scores", ["quick_ratio_score"])

    merged = (
        net_debt_ebitda_scores[["ticker", "date", "net_debt_to_ebitda_score"]]
        .merge(
            interest_coverage_scores[["ticker", "date", "interest_coverage_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            current_ratio_scores[["ticker", "date", "current_ratio_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            quick_ratio_scores[["ticker", "date", "quick_ratio_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "net_debt_to_ebitda_score": net_debt_ebitda_weight,
        "interest_coverage_score": interest_coverage_weight,
        "current_ratio_score": current_ratio_weight,
        "quick_ratio_score": quick_ratio_weight,
    }
    result = blend_scores(merged, weights, "financial_fortress_score")
    result = result.dropna(subset=["financial_fortress_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "financial_fortress_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        net_debt_ebitda_weight=net_debt_ebitda_weight,
        interest_coverage_weight=interest_coverage_weight,
        current_ratio_weight=current_ratio_weight,
        quick_ratio_weight=quick_ratio_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

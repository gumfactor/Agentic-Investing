"""Income Yield composite signal.

Combines total shareholder yield, dividend yield, and buyback yield to
identify stocks that return the most cash to shareholders — the income and
capital-return premium.

The thesis: firms that consistently return cash to shareholders via dividends
and buybacks signal financial health, management alignment with owners, and
often a lower cost of equity (Boudoukh et al. 2007). Total shareholder yield
(dividends + net buybacks as a fraction of market cap) is the broadest measure
and subsumes both channels. Including dividend yield and buyback yield as
separate components provides granular views on income (dividend) versus capital
structure management (buybacks), allowing the composite to give appropriate
credit to firms that rely more heavily on either channel.

The three signals are complementary:
  (a) shareholder_yield: dividends + net buybacks as % of market cap. The
      broadest capital-return measure. Captures both income and buyback-driven
      returns in one metric.
  (b) dividend_yield: dividend per share / price. Classic income signal and
      the primary metric for income-focused mandates such as dividend ETFs
      and pension funds.
  (c) buyback_yield: net share repurchases as % of market cap. Complements
      the dividend channel — many firms prefer buybacks for tax efficiency
      and capital flexibility. High buyback yield alone signals management
      confidence and capital discipline.

All three signals are positive-direction (higher = more cash returned) and
require no internal negation.

Basis: Boudoukh, Michaely, Richardson & Roberts (2007) "On the Importance
of Measuring Payout Yield"; Blitz, Huij & Martens (2011) on dividend yield
and momentum interaction; Ikenberry, Lakonishok & Vermaelen (1995) on
open-market share repurchases.

Default weighting: shareholder yield 40%, dividend yield 35%, buyback yield
25%. Shareholder yield is the broadest and most predictive single metric
(Boudoukh et al. 2007); dividend yield carries the most weight among income-
seekers; buyback yield adds capital-efficiency information.

Inputs
------
shareholder_yield_scores: Output of compute_shareholder_yield_scores(). Must
    contain ``ticker``, ``date``, ``shareholder_yield_score``.
dividend_yield_scores: Output of compute_dividend_yield_scores(). Must
    contain ``ticker``, ``date``, ``dividend_yield_score``.
buyback_yield_scores: Output of compute_buyback_yield_scores(). Must
    contain ``ticker``, ``date``, ``buyback_yield_score``.

Output
------
income_yield_score: weighted blend, cross-sectionally re-standardized per
date. Higher = high total shareholder yield + high dividend income + active
buyback program (comprehensive capital-return strength).
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_income_yield_scores(
    shareholder_yield_scores: pd.DataFrame,
    dividend_yield_scores: pd.DataFrame,
    buyback_yield_scores: pd.DataFrame,
    shareholder_yield_weight: float = 0.40,
    dividend_yield_weight: float = 0.35,
    buyback_yield_weight: float = 0.25,
) -> pd.DataFrame:
    """Blend shareholder yield, dividend yield, and buyback yield.

    Args:
        shareholder_yield_scores: Output of compute_shareholder_yield_scores().
            Must contain columns ``ticker``, ``date``,
            ``shareholder_yield_score``.
        dividend_yield_scores: Output of compute_dividend_yield_scores(). Must
            contain columns ``ticker``, ``date``, ``dividend_yield_score``.
        buyback_yield_scores: Output of compute_buyback_yield_scores(). Must
            contain columns ``ticker``, ``date``, ``buyback_yield_score``.
        shareholder_yield_weight: Relative weight for total shareholder yield.
            Default 0.40.
        dividend_yield_weight: Relative weight for dividend yield. Default 0.35.
        buyback_yield_weight: Relative weight for buyback yield. Default 0.25.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``shareholder_yield_score``,
            ``dividend_yield_score``, ``buyback_yield_score``,
            ``income_yield_score``

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(
        shareholder_yield_scores, "shareholder_yield_scores", ["shareholder_yield_score"]
    )
    _validate(dividend_yield_scores, "dividend_yield_scores", ["dividend_yield_score"])
    _validate(buyback_yield_scores, "buyback_yield_scores", ["buyback_yield_score"])

    merged = (
        shareholder_yield_scores[["ticker", "date", "shareholder_yield_score"]]
        .merge(
            dividend_yield_scores[["ticker", "date", "dividend_yield_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            buyback_yield_scores[["ticker", "date", "buyback_yield_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "shareholder_yield_score": shareholder_yield_weight,
        "dividend_yield_score": dividend_yield_weight,
        "buyback_yield_score": buyback_yield_weight,
    }
    result = blend_scores(merged, weights, "income_yield_score")

    result = result.dropna(subset=["income_yield_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "income_yield_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        shareholder_yield_weight=shareholder_yield_weight,
        dividend_yield_weight=dividend_yield_weight,
        buyback_yield_weight=buyback_yield_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

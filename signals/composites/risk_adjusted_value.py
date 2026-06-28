"""Risk-Adjusted Value composite signal.

Combines fundamental cheapness with a long-run risk-adjusted return screen
to reduce exposure to value traps — cheap stocks that are cheap because their
business is genuinely deteriorating.

The thesis: raw value investing faces the "value trap" problem — a stock
can be cheap because it deserves to be cheap. Adding a Sharpe ratio filter
distinguishes value traps (cheap + poor risk-adjusted return history) from
genuine value opportunities (cheap + history of reasonable returns despite
being undervalued). An additional drawdown screen avoids stocks experiencing
severe technical breakdown, which often precede further fundamental
deterioration. Blitz & van Vliet (2007) document that low-risk value
portfolios outperform high-risk value portfolios; Novy-Marx (2014) shows
that combining value with quality characteristics significantly reduces
value trap exposure.

The three signals are mutually reinforcing:
  (a) value_score: composite fundamental value — earnings yield, book/
      price, FCF yield, EV multiples, shareholder yield. Higher = cheaper.
  (b) sharpe_ratio_252d: annual risk-adjusted return over 252 days. Filters
      out stocks whose historical returns per unit of risk are chronically
      poor — a hallmark of value traps.
  (c) max_drawdown_63d: worst peak-to-trough loss over 63 days, negated.
      Avoids stocks currently in severe drawdown, which often signals active
      fundamental deterioration rather than temporary mispricing.

The drawdown signal is negated internally (lower raw drawdown magnitude =
better = higher composite score contribution). Raw values are preserved in
the output for transparency.

Default weighting: value 50%, Sharpe 30%, low-drawdown 20%.
Value remains the primary fundamental anchor; the risk screens are secondary
quality-of-value filters.

Inputs
------
value_scores: Output of compute_value_scores(). Must contain ``ticker``,
    ``date``, ``value_score``.
sharpe_scores: Output of compute_sharpe_ratio_252d_scores(). Must contain
    ``ticker``, ``date``, ``sharpe_ratio_252d_score``.
drawdown_scores: Output of compute_max_drawdown_63d_scores(). Must contain
    ``ticker``, ``date``, ``max_drawdown_63d_score``.

Output
------
risk_adjusted_value_score: weighted blend, cross-sectionally re-standardized
per date. Higher = fundamentally cheap + solid long-run risk-adjusted return
history + limited recent drawdown.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_risk_adjusted_value_scores(
    value_scores: pd.DataFrame,
    sharpe_scores: pd.DataFrame,
    drawdown_scores: pd.DataFrame,
    value_weight: float = 0.50,
    sharpe_weight: float = 0.30,
    drawdown_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend value with Sharpe ratio and inverted max drawdown.

    Args:
        value_scores: Output of compute_value_scores(). Must contain
            columns ``ticker``, ``date``, ``value_score``.
        sharpe_scores: Output of compute_sharpe_ratio_252d_scores(). Must
            contain columns ``ticker``, ``date``, ``sharpe_ratio_252d_score``.
        drawdown_scores: Output of compute_max_drawdown_63d_scores(). Must
            contain columns ``ticker``, ``date``, ``max_drawdown_63d_score``.
        value_weight: Relative weight for value. Default 0.50.
        sharpe_weight: Relative weight for Sharpe ratio. Default 0.30.
        drawdown_weight: Relative weight for the low-drawdown signal
            (inverted max drawdown). Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``value_score``,
            ``sharpe_ratio_252d_score``, ``max_drawdown_63d_score``,
            ``risk_adjusted_value_score``

        ``max_drawdown_63d_score`` is negated internally so that lower
        drawdown magnitude contributes a higher composite score. The
        original raw value is preserved in the output column.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(value_scores, "value_scores", ["value_score"])
    _validate(sharpe_scores, "sharpe_scores", ["sharpe_ratio_252d_score"])
    _validate(drawdown_scores, "drawdown_scores", ["max_drawdown_63d_score"])

    merged = (
        value_scores[["ticker", "date", "value_score"]]
        .merge(
            sharpe_scores[["ticker", "date", "sharpe_ratio_252d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            drawdown_scores[["ticker", "date", "max_drawdown_63d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    # Negate drawdown: lower drawdown magnitude = less distressed = higher score
    merged = merged.copy()
    merged["_drawdown_protected"] = -merged["max_drawdown_63d_score"]

    weights = {
        "value_score": value_weight,
        "sharpe_ratio_252d_score": sharpe_weight,
        "_drawdown_protected": drawdown_weight,
    }
    result = blend_scores(merged, weights, "risk_adjusted_value_score")

    result = result.drop(columns=["_drawdown_protected"])
    result = result.dropna(subset=["risk_adjusted_value_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "risk_adjusted_value_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        value_weight=value_weight,
        sharpe_weight=sharpe_weight,
        drawdown_weight=drawdown_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

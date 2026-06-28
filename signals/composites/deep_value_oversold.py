"""Deep Value + Oversold composite signal.

Combines fundamental cheapness (value factor) with two independent technical
oversold signals to improve the timing of value entries.

The thesis: pure value investing faces the "value trap" problem — cheap stocks
can stay cheap for years while the investor bleeds opportunity cost. Adding a
technical oversold filter helps time the entry: wait for the market to
temporarily push the price even lower than fundamental value suggests, then
buy when selling exhaustion appears. This is the quantitative analog of "buy
when there's blood in the streets."

The two oversold signals are complementary:
  (a) RSI(14) raw: short-term buying/selling pressure in absolute terms.
      RSI < 30 traditionally signals selling exhaustion.
  (b) Bollinger %B(20) raw: where price sits within its recent trading range.
      %B < 0.2 means the price is near or below the lower Bollinger Band —
      a short-to-medium-term oversold condition.

Both oscillator signals are negated internally (lower raw value = more
oversold = higher composite score contribution). The value dimension uses the
pre-computed composite value_score, which is already cross-sectionally
z-scored and higher = cheaper.

Basis: O'Shaughnessy "What Works on Wall Street" on combined value + technical
screens; Novy-Marx on value factor timing; practitioner mean-reversion
literature combining RSI / Bollinger oversold signals with fundamental screens.

Default weighting: value_score 50%, rsi_oversold 30%, bb_oversold 20%.
Value carries the most weight as the primary fundamental anchor; the two
technical signals are secondary timing filters.

Inputs
------
value_scores: Output of compute_value_scores(). Must contain
    ``ticker``, ``date``, ``value_score``.
rsi_raw_scores: Output of compute_rsi_14_raw_scores(). Must contain
    ``ticker``, ``date``, ``rsi_14_raw``.
bb_pct_b_raw_scores: Output of compute_bb_pct_b_20_raw_scores(). Must
    contain ``ticker``, ``date``, ``bb_pct_b_20_raw``.

Output
------
deep_value_oversold_score: weighted blend, cross-sectionally re-standardized
per date. Higher = fundamentally cheap AND showing multiple technical oversold
signals.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_deep_value_oversold_scores(
    value_scores: pd.DataFrame,
    rsi_raw_scores: pd.DataFrame,
    bb_pct_b_raw_scores: pd.DataFrame,
    value_weight: float = 0.50,
    rsi_weight: float = 0.30,
    bb_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend value with inverted RSI and Bollinger %B into a deep-value-oversold composite.

    Args:
        value_scores: Output of compute_value_scores(). Must contain
            columns ``ticker``, ``date``, ``value_score``.
        rsi_raw_scores: Output of compute_rsi_14_raw_scores(). Must contain
            columns ``ticker``, ``date``, ``rsi_14_raw``.
        bb_pct_b_raw_scores: Output of compute_bb_pct_b_20_raw_scores(). Must
            contain columns ``ticker``, ``date``, ``bb_pct_b_20_raw``.
        value_weight: Relative weight for value. Default 0.50.
        rsi_weight: Relative weight for RSI oversold signal. Default 0.30.
        bb_weight: Relative weight for Bollinger %B oversold signal.
            Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``value_score``, ``rsi_14_raw``,
            ``bb_pct_b_20_raw``, ``deep_value_oversold_score``

        The RSI and %B values are negated internally before blending so that
        lower raw values (more oversold) contribute a higher score. The
        original raw values are preserved in the output for transparency.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(value_scores, "value_scores", ["value_score"])
    _validate(rsi_raw_scores, "rsi_raw_scores", ["rsi_14_raw"])
    _validate(bb_pct_b_raw_scores, "bb_pct_b_raw_scores", ["bb_pct_b_20_raw"])

    merged = (
        value_scores[["ticker", "date", "value_score"]]
        .merge(
            rsi_raw_scores[["ticker", "date", "rsi_14_raw"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            bb_pct_b_raw_scores[["ticker", "date", "bb_pct_b_20_raw"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    # Negate raw oscillator values: lower %B / lower RSI = more oversold = higher score
    merged = merged.copy()
    merged["_rsi_oversold"] = -merged["rsi_14_raw"]
    merged["_bb_oversold"] = -merged["bb_pct_b_20_raw"]

    weights = {
        "value_score": value_weight,
        "_rsi_oversold": rsi_weight,
        "_bb_oversold": bb_weight,
    }
    result = blend_scores(merged, weights, "deep_value_oversold_score")

    # Drop internal negated columns; retain original raw values for transparency
    result = result.drop(columns=["_rsi_oversold", "_bb_oversold"])
    result = result.dropna(subset=["deep_value_oversold_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "deep_value_oversold_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        value_weight=value_weight,
        rsi_weight=rsi_weight,
        bb_weight=bb_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

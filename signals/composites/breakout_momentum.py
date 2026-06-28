"""Breakout Momentum composite signal.

Combines three price-structure signals that together identify stocks in
confirmed technical breakouts — trading near multi-period highs, in the
upper portion of their recent range, and above a rising long-term trend.

The thesis: stocks approaching or setting new highs face an "anchoring"
resistance from investors who bought at the old high and have been waiting
to break even. Once that level is cleared, those sellers disappear and
institutional momentum traders accelerate the move. George & Hwang (2004)
show that 52-week high proximity is one of the most robust predictors of
near-term outperformance, distinct from and additive to simple return-based
momentum.

The three signals are complementary across different time horizons:
  (a) price_vs_52w_high: where price sits relative to its 252-day rolling
      high — the canonical breakout metric. High score = stock near or at
      its annual high, clearing anchoring resistance.
  (b) donchian_pct: where price sits within its 63-day high/low channel.
      Captures intermediate (3-month) momentum independent of the 52-week
      context. A stock that is near the top of its quarterly channel but
      still below its annual high is in a building breakout.
  (c) ma_cross_50_200: the 50-day vs 200-day MA spread (golden cross
      strength). Confirms that the longer-term trend is already constructive
      before the breakout signal fires, reducing false-positive breakouts
      in downtrending sectors.

All three signals are higher = stronger breakout; no negation is required.

Basis: George & Hwang (2004) "The 52-Week High and Momentum Investing";
Donchian channel methodology from turtle-trading literature; golden cross
as a trend-quality filter.

Default weighting: 52-week high proximity 40%, Donchian position 35%,
MA cross 25%. The two range-proximity signals dominate; MA cross adds
structural confirmation.

Inputs
------
price_vs_52w_high_scores: Output of compute_price_vs_52w_high_scores().
    Must contain ``ticker``, ``date``, ``price_vs_52w_high_score``.
donchian_scores: Output of compute_donchian_pct_scores(). Must contain
    ``ticker``, ``date``, ``donchian_pct_score``.
ma_cross_scores: Output of compute_ma_cross_50_200_scores(). Must contain
    ``ticker``, ``date``, ``ma_cross_50_200_score``.

Output
------
breakout_momentum_score: weighted blend, cross-sectionally re-standardized
per date. Higher = price near 52-week high + in upper Donchian channel +
above rising 200-day MA (confirmed breakout setup).
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_breakout_momentum_scores(
    price_vs_52w_high_scores: pd.DataFrame,
    donchian_scores: pd.DataFrame,
    ma_cross_scores: pd.DataFrame,
    price_vs_52w_high_weight: float = 0.40,
    donchian_weight: float = 0.35,
    ma_cross_weight: float = 0.25,
) -> pd.DataFrame:
    """Blend 52-week high proximity, Donchian position, and MA cross.

    Args:
        price_vs_52w_high_scores: Output of compute_price_vs_52w_high_scores().
            Must contain columns ``ticker``, ``date``,
            ``price_vs_52w_high_score``.
        donchian_scores: Output of compute_donchian_pct_scores(). Must
            contain columns ``ticker``, ``date``, ``donchian_pct_score``.
        ma_cross_scores: Output of compute_ma_cross_50_200_scores(). Must
            contain columns ``ticker``, ``date``, ``ma_cross_50_200_score``.
        price_vs_52w_high_weight: Relative weight for 52-week high proximity.
            Default 0.40.
        donchian_weight: Relative weight for Donchian channel position.
            Default 0.35.
        ma_cross_weight: Relative weight for 50/200-day MA cross.
            Default 0.25.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``price_vs_52w_high_score``,
            ``donchian_pct_score``, ``ma_cross_50_200_score``,
            ``breakout_momentum_score``

        All three signals point in the same direction (higher = stronger
        breakout); no internal negation is required.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(price_vs_52w_high_scores, "price_vs_52w_high_scores", ["price_vs_52w_high_score"])
    _validate(donchian_scores, "donchian_scores", ["donchian_pct_score"])
    _validate(ma_cross_scores, "ma_cross_scores", ["ma_cross_50_200_score"])

    merged = (
        price_vs_52w_high_scores[["ticker", "date", "price_vs_52w_high_score"]]
        .merge(
            donchian_scores[["ticker", "date", "donchian_pct_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            ma_cross_scores[["ticker", "date", "ma_cross_50_200_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "price_vs_52w_high_score": price_vs_52w_high_weight,
        "donchian_pct_score": donchian_weight,
        "ma_cross_50_200_score": ma_cross_weight,
    }
    result = blend_scores(merged, weights, "breakout_momentum_score")

    result = result.dropna(subset=["breakout_momentum_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "breakout_momentum_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        price_vs_52w_high_weight=price_vs_52w_high_weight,
        donchian_weight=donchian_weight,
        ma_cross_weight=ma_cross_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

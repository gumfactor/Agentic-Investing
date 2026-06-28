"""Small-Cap Quality composite signal.

Combines the small-cap size premium with fundamental quality and a
volatility filter to capture the small-cap effect while avoiding the
low-quality, high-volatility names that typically dominate a naive
small-cap screen.

The thesis: the Fama-French (1993) small-minus-big (SMB) factor documents
that smaller firms earn higher long-run returns. However, the small-cap
universe is dominated by unprofitable, highly-levered, and illiquid names
that drive much of the realized risk but little of the premium. Novy-Marx &
Velikov (2014) and Israel & Moskowitz (2013) show that the small-cap premium
concentrates among the highest-quality small-cap firms. Filtering for quality
and low realized volatility targets the premium at its source while shedding
most of the idiosyncratic risk.

The three signals are complementary:
  (a) log_market_cap: −ln(Price × Shares), negated at the indicator level
      so higher = smaller firm. The primary size signal; captures the Fama-
      French SMB factor on a cross-sectional z-score basis.
  (b) quality_score: composite fundamental quality across profitability,
      leverage, and earnings quality. Filters out the low-quality small-cap
      cohort that dominates naive size screens.
  (c) realized_vol_21d: 21-day realized volatility, negated internally.
      Small-cap stocks are naturally more volatile; this secondary filter
      prefers those that are smaller-than-average while being less volatile
      than their small-cap peers — a proxy for higher liquidity and more
      institutional ownership.

The realized volatility signal is negated internally (lower vol = more
stable = higher composite score contribution). The original raw value is
preserved in the output.

Basis: Fama & French (1993) "Common Risk Factors in the Returns on Stocks
and Bonds"; Novy-Marx & Velikov (2014) on quality-screened factor returns;
Israel & Moskowitz (2013) on size anomaly anatomy.

Default weighting: small-cap size 50%, quality 30%, low-volatility filter
20%. Size is the primary thesis; quality and vol are secondary filters that
target the premium more precisely.

Inputs
------
market_cap_scores: Output of compute_log_market_cap_scores(). Must contain
    ``ticker``, ``date``, ``log_market_cap_score``.
quality_scores: Output of compute_quality_scores(). Must contain ``ticker``,
    ``date``, ``quality_score``.
realized_vol_scores: Output of compute_realized_vol_21d_scores(). Must
    contain ``ticker``, ``date``, ``realized_vol_21d_score``.

Output
------
small_cap_quality_score: weighted blend, cross-sectionally re-standardized
per date. Higher = small-cap + high fundamental quality + low recent
volatility (targeting the quality-screened small-cap premium).
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_small_cap_quality_scores(
    market_cap_scores: pd.DataFrame,
    quality_scores: pd.DataFrame,
    realized_vol_scores: pd.DataFrame,
    market_cap_weight: float = 0.50,
    quality_weight: float = 0.30,
    realized_vol_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend small-cap size with quality and a low-volatility filter.

    Args:
        market_cap_scores: Output of compute_log_market_cap_scores(). Must
            contain columns ``ticker``, ``date``, ``log_market_cap_score``.
            Note: this indicator is already negated at the source so higher
            score = smaller firm.
        quality_scores: Output of compute_quality_scores(). Must contain
            columns ``ticker``, ``date``, ``quality_score``.
        realized_vol_scores: Output of compute_realized_vol_21d_scores().
            Must contain columns ``ticker``, ``date``,
            ``realized_vol_21d_score``.
        market_cap_weight: Relative weight for size (small-cap). Default 0.50.
        quality_weight: Relative weight for quality. Default 0.30.
        realized_vol_weight: Relative weight for the low-vol filter (inverted
            realized volatility). Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``log_market_cap_score``,
            ``quality_score``, ``realized_vol_21d_score``,
            ``small_cap_quality_score``

        ``realized_vol_21d_score`` is negated internally so that lower
        volatility contributes a higher composite score. The original raw
        value is preserved in the output column.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(market_cap_scores, "market_cap_scores", ["log_market_cap_score"])
    _validate(quality_scores, "quality_scores", ["quality_score"])
    _validate(realized_vol_scores, "realized_vol_scores", ["realized_vol_21d_score"])

    merged = (
        market_cap_scores[["ticker", "date", "log_market_cap_score"]]
        .merge(
            quality_scores[["ticker", "date", "quality_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            realized_vol_scores[["ticker", "date", "realized_vol_21d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    # Negate realized vol: lower volatility = more stable small-cap = higher score
    merged = merged.copy()
    merged["_low_vol"] = -merged["realized_vol_21d_score"]

    weights = {
        "log_market_cap_score": market_cap_weight,
        "quality_score": quality_weight,
        "_low_vol": realized_vol_weight,
    }
    result = blend_scores(merged, weights, "small_cap_quality_score")

    result = result.drop(columns=["_low_vol"])
    result = result.dropna(subset=["small_cap_quality_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "small_cap_quality_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        market_cap_weight=market_cap_weight,
        quality_weight=quality_weight,
        realized_vol_weight=realized_vol_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

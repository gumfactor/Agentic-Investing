"""Low-Volatility Momentum composite signal.

Combines risk-adjusted momentum with a low-volatility tilt to reduce the
crash risk that plagues pure momentum strategies.

The thesis: raw momentum works but suffers periodic violent reversals —
"momentum crashes" (Daniel & Moskowitz 2016) — concentrated in high-vol,
high-beta stocks. Filtering to low-volatility momentum stocks captures most
of the momentum premium while shedding the tail risk. Frazzini & Pedersen
(2014) document that low-beta assets earn higher risk-adjusted returns than
the CAPM predicts; Baker et al. (2011) show the low-volatility anomaly
holds across global markets.

The three signals are mutually reinforcing:
  (a) vol_adjusted_mom_12m: 12-month momentum divided by 252-day realized
      volatility — stocks with strong momentum AND low volatility score
      highest.  Already the primary risk-adjusted momentum signal.
  (b) realized_vol_21d: recent (21-day) volatility, negated — reduces
      exposure to vol regime spikes not yet reflected in the 252-day window.
  (c) sortino_ratio_63d: quarterly downside-risk-adjusted return — filters
      for stocks where gains consistently exceed downside moves.

The volatility signal is negated internally (lower raw value = less
volatile = higher score contribution). Raw values are preserved in the
output for transparency.

Default weighting: vol-adj momentum 50%, low-vol 30%, Sortino 20%.
Momentum carries the most weight as the primary signal; vol suppression
provides the defensive overlay.

Inputs
------
vol_adj_mom_scores: Output of compute_vol_adjusted_mom_12m_scores(). Must
    contain ``ticker``, ``date``, ``vol_adjusted_mom_12m_score``.
realized_vol_scores: Output of compute_realized_vol_21d_scores(). Must
    contain ``ticker``, ``date``, ``realized_vol_21d_score``.
sortino_scores: Output of compute_sortino_ratio_63d_scores(). Must contain
    ``ticker``, ``date``, ``sortino_ratio_63d_score``.

Output
------
low_vol_momentum_score: weighted blend, cross-sectionally re-standardized
per date. Higher = strong risk-adjusted momentum + low recent volatility +
good downside-risk-adjusted return.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_low_vol_momentum_scores(
    vol_adj_mom_scores: pd.DataFrame,
    realized_vol_scores: pd.DataFrame,
    sortino_scores: pd.DataFrame,
    vol_adj_mom_weight: float = 0.50,
    realized_vol_weight: float = 0.30,
    sortino_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend vol-adjusted momentum with low-vol and Sortino filters.

    Args:
        vol_adj_mom_scores: Output of compute_vol_adjusted_mom_12m_scores().
            Must contain columns ``ticker``, ``date``,
            ``vol_adjusted_mom_12m_score``.
        realized_vol_scores: Output of compute_realized_vol_21d_scores().
            Must contain columns ``ticker``, ``date``,
            ``realized_vol_21d_score``.
        sortino_scores: Output of compute_sortino_ratio_63d_scores(). Must
            contain columns ``ticker``, ``date``, ``sortino_ratio_63d_score``.
        vol_adj_mom_weight: Relative weight for vol-adjusted momentum.
            Default 0.50.
        realized_vol_weight: Relative weight for the low-vol signal (inverted
            realized vol). Default 0.30.
        sortino_weight: Relative weight for the Sortino ratio signal.
            Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``vol_adjusted_mom_12m_score``,
            ``realized_vol_21d_score``, ``sortino_ratio_63d_score``,
            ``low_vol_momentum_score``

        ``realized_vol_21d_score`` is negated internally so that lower
        realized volatility contributes a higher composite score. The
        original raw value is preserved in the output column.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(vol_adj_mom_scores, "vol_adj_mom_scores", ["vol_adjusted_mom_12m_score"])
    _validate(realized_vol_scores, "realized_vol_scores", ["realized_vol_21d_score"])
    _validate(sortino_scores, "sortino_scores", ["sortino_ratio_63d_score"])

    merged = (
        vol_adj_mom_scores[["ticker", "date", "vol_adjusted_mom_12m_score"]]
        .merge(
            realized_vol_scores[["ticker", "date", "realized_vol_21d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            sortino_scores[["ticker", "date", "sortino_ratio_63d_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    # Negate realized vol: lower volatility = less risky = higher score
    merged = merged.copy()
    merged["_low_vol"] = -merged["realized_vol_21d_score"]

    weights = {
        "vol_adjusted_mom_12m_score": vol_adj_mom_weight,
        "_low_vol": realized_vol_weight,
        "sortino_ratio_63d_score": sortino_weight,
    }
    result = blend_scores(merged, weights, "low_vol_momentum_score")

    result = result.drop(columns=["_low_vol"])
    result = result.dropna(subset=["low_vol_momentum_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "low_vol_momentum_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        vol_adj_mom_weight=vol_adj_mom_weight,
        realized_vol_weight=realized_vol_weight,
        sortino_weight=sortino_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

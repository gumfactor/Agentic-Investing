"""Growth Momentum composite signal.

Combines fundamental growth with price momentum confirmation to capture
stocks where improving business fundamentals are being recognized in the
market — the quantitative analog of the "CAN SLIM" growth + momentum
strategy.

The thesis: fundamental growth alone does not predict near-term returns if
the market has already priced in the growth trajectory. Price momentum
provides independent confirmation that the market is currently validating
the growth story. Stocks with accelerating earnings AND rising prices are
in a virtuous cycle where institutional investors keep buying as each
earnings report confirms the thesis. Conversely, strong growth without
price follow-through can indicate a "growth trap" — growth that is real
but already priced in or distracted by sector headwinds.

The three signals span fundamental quality of growth, market validation,
and the pace of change:
  (a) growth_score: composite growth across revenue, EPS, FCF, margin
      expansion, and EPS acceleration. Fundamental growth anchor.
  (b) vol_adjusted_mom_12m: 12-month momentum divided by realized
      volatility. Provides price-based confirmation and filters for
      stocks whose momentum is risk-adjusted (avoids chasing high-vol
      momentum names that can reverse violently).
  (c) eps_growth_acceleration: second derivative of earnings — whether
      the growth rate itself is speeding up. Acts as the "current
      catalyst" signal: accelerating earnings often precede positive
      earnings surprise re-ratings.

Basis: O'Neil "How to Make Money in Stocks" (CAN SLIM); Novy-Marx
"Fundamentally, Momentum is Fundamental Momentum" (2015); empirical
evidence on earnings surprise momentum and post-earnings announcement
drift.

Default weighting: growth_score 50%, vol-adjusted momentum 30%, EPS
acceleration 20%. Growth leads; momentum confirms; acceleration provides
the timing signal.

Inputs
------
growth_scores: Output of compute_growth_scores(). Must contain ``ticker``,
    ``date``, ``growth_score``.
vol_adj_mom_scores: Output of compute_vol_adjusted_mom_12m_scores(). Must
    contain ``ticker``, ``date``, ``vol_adjusted_mom_12m_score``.
eps_acceleration_scores: Output of compute_eps_growth_acceleration_scores().
    Must contain ``ticker``, ``date``, ``eps_growth_acceleration_score``.

Output
------
growth_momentum_score: weighted blend, cross-sectionally re-standardized
per date. Higher = strong durable fundamental growth + market price
confirmation + accelerating earnings trajectory.
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_growth_momentum_scores(
    growth_scores: pd.DataFrame,
    vol_adj_mom_scores: pd.DataFrame,
    eps_acceleration_scores: pd.DataFrame,
    growth_weight: float = 0.50,
    vol_adj_mom_weight: float = 0.30,
    eps_acceleration_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend growth with vol-adjusted momentum and EPS acceleration.

    Args:
        growth_scores: Output of compute_growth_scores(). Must contain
            columns ``ticker``, ``date``, ``growth_score``.
        vol_adj_mom_scores: Output of
            compute_vol_adjusted_mom_12m_scores(). Must contain columns
            ``ticker``, ``date``, ``vol_adjusted_mom_12m_score``.
        eps_acceleration_scores: Output of
            compute_eps_growth_acceleration_scores(). Must contain columns
            ``ticker``, ``date``, ``eps_growth_acceleration_score``.
        growth_weight: Relative weight for the growth composite.
            Default 0.50.
        vol_adj_mom_weight: Relative weight for vol-adjusted momentum.
            Default 0.30.
        eps_acceleration_weight: Relative weight for EPS growth
            acceleration. Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``growth_score``,
            ``vol_adjusted_mom_12m_score``, ``eps_growth_acceleration_score``,
            ``growth_momentum_score``

        All three signals point higher = better; no internal negation is
        required.

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(growth_scores, "growth_scores", ["growth_score"])
    _validate(vol_adj_mom_scores, "vol_adj_mom_scores", ["vol_adjusted_mom_12m_score"])
    _validate(eps_acceleration_scores, "eps_acceleration_scores", ["eps_growth_acceleration_score"])

    merged = (
        growth_scores[["ticker", "date", "growth_score"]]
        .merge(
            vol_adj_mom_scores[["ticker", "date", "vol_adjusted_mom_12m_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            eps_acceleration_scores[["ticker", "date", "eps_growth_acceleration_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "growth_score": growth_weight,
        "vol_adjusted_mom_12m_score": vol_adj_mom_weight,
        "eps_growth_acceleration_score": eps_acceleration_weight,
    }
    result = blend_scores(merged, weights, "growth_momentum_score")

    result = result.dropna(subset=["growth_momentum_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "growth_momentum_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        growth_weight=growth_weight,
        vol_adj_mom_weight=vol_adj_mom_weight,
        eps_acceleration_weight=eps_acceleration_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

"""Small-Cap Momentum composite signal.

Combines the small-cap size premium with risk-adjusted momentum and relative
strength to capture the interaction between the size and momentum factors —
the small-cap segment where momentum is historically the strongest.

The thesis: Fama & French (1993) document the size premium (SMB); Jegadeesh
& Titman (1993) document the momentum premium. The two interact: momentum
is strongest within the small-cap universe because smaller stocks have less
analyst coverage, slower information diffusion, and greater under-reaction
to positive news (Hong & Stein 1999). A composite that simultaneously
requires small-cap status and recent outperformance screens for the most
fertile region of the momentum anomaly.

The three signals are complementary:
  (a) log_market_cap: −ln(Price × Shares), negated at the indicator level
      so higher = smaller firm. Already negated at source; no internal
      negation needed here. The primary size gate.
  (b) vol_adjusted_mom_12m: volatility-adjusted 12-month momentum (return /
      trailing volatility). Risk-adjusting raw momentum improves the
      signal-to-noise ratio and penalizes lottery-ticket small-caps that
      show raw momentum driven solely by speculative spikes.
  (c) rel_strength_vs_spy_12m: 12-month return relative to SPY. Cross-
      sectional outperformance versus the broad market confirms that the
      small-cap momentum candidate is genuinely beating the market, not
      just rising with the tide.

Both momentum signals are positive-direction (higher = better momentum) and
require no internal negation.

Basis: Fama & French (1993) "Common Risk Factors in the Returns on Stocks
and Bonds"; Jegadeesh & Titman (1993) "Returns to Buying Winners and Selling
Losers"; Hong & Stein (1999) on gradual-information-diffusion momentum.

Default weighting: small-cap size 50%, vol-adjusted momentum 30%, relative
strength 20%. Size is the primary screen; momentum signals select the
strongest-trending names within the small-cap cohort.

Inputs
------
market_cap_scores: Output of compute_log_market_cap_scores(). Must contain
    ``ticker``, ``date``, ``log_market_cap_score``.
vol_adj_mom_scores: Output of compute_vol_adjusted_mom_12m_scores(). Must
    contain ``ticker``, ``date``, ``vol_adjusted_mom_12m_score``.
rel_strength_12m_scores: Output of compute_rel_strength_vs_spy_12m_scores().
    Must contain ``ticker``, ``date``, ``rel_strength_vs_spy_12m_score``.

Output
------
small_cap_momentum_score: weighted blend, cross-sectionally re-standardized
per date. Higher = small-cap + strong vol-adjusted 12m momentum + strong
12m outperformance vs SPY (small-cap momentum sweet spot).
"""

from __future__ import annotations

import pandas as pd
import structlog

from signals.composites._blend import blend_scores

logger = structlog.get_logger(__name__)


def compute_small_cap_momentum_scores(
    market_cap_scores: pd.DataFrame,
    vol_adj_mom_scores: pd.DataFrame,
    rel_strength_12m_scores: pd.DataFrame,
    market_cap_weight: float = 0.50,
    vol_adj_mom_weight: float = 0.30,
    rel_strength_12m_weight: float = 0.20,
) -> pd.DataFrame:
    """Blend small-cap size with vol-adjusted momentum and relative strength.

    Args:
        market_cap_scores: Output of compute_log_market_cap_scores(). Must
            contain columns ``ticker``, ``date``, ``log_market_cap_score``.
            Note: this indicator is already negated at the source so higher
            score = smaller firm.
        vol_adj_mom_scores: Output of compute_vol_adjusted_mom_12m_scores().
            Must contain columns ``ticker``, ``date``,
            ``vol_adjusted_mom_12m_score``.
        rel_strength_12m_scores: Output of
            compute_rel_strength_vs_spy_12m_scores(). Must contain columns
            ``ticker``, ``date``, ``rel_strength_vs_spy_12m_score``.
        market_cap_weight: Relative weight for size (small-cap). Default 0.50.
        vol_adj_mom_weight: Relative weight for vol-adjusted momentum.
            Default 0.30.
        rel_strength_12m_weight: Relative weight for 12m relative strength vs
            SPY. Default 0.20.

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``log_market_cap_score``,
            ``vol_adjusted_mom_12m_score``, ``rel_strength_vs_spy_12m_score``,
            ``small_cap_momentum_score``

        Rows present in only some inputs are retained with NaN for missing
        dimensions; their weight is redistributed to available signals.
        Rows where all inputs are NaN are dropped.
    """
    _validate(market_cap_scores, "market_cap_scores", ["log_market_cap_score"])
    _validate(vol_adj_mom_scores, "vol_adj_mom_scores", ["vol_adjusted_mom_12m_score"])
    _validate(
        rel_strength_12m_scores,
        "rel_strength_12m_scores",
        ["rel_strength_vs_spy_12m_score"],
    )

    merged = (
        market_cap_scores[["ticker", "date", "log_market_cap_score"]]
        .merge(
            vol_adj_mom_scores[["ticker", "date", "vol_adjusted_mom_12m_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .merge(
            rel_strength_12m_scores[["ticker", "date", "rel_strength_vs_spy_12m_score"]],
            on=["ticker", "date"],
            how="outer",
        )
        .reset_index(drop=True)
    )

    weights = {
        "log_market_cap_score": market_cap_weight,
        "vol_adjusted_mom_12m_score": vol_adj_mom_weight,
        "rel_strength_vs_spy_12m_score": rel_strength_12m_weight,
    }
    result = blend_scores(merged, weights, "small_cap_momentum_score")

    result = result.dropna(subset=["small_cap_momentum_score"])
    result = result.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "small_cap_momentum_scores_computed",
        dates=result["date"].nunique(),
        tickers=result["ticker"].nunique(),
        market_cap_weight=market_cap_weight,
        vol_adj_mom_weight=vol_adj_mom_weight,
        rel_strength_12m_weight=rel_strength_12m_weight,
    )
    return result


def _validate(df: pd.DataFrame, name: str, required_cols: list[str]) -> None:
    base = {"ticker", "date"}
    missing = (base | set(required_cols)) - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {missing}")

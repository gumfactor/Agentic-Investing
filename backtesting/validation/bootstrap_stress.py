"""Permutation stress test for surviving strategies.

Reshuffles a strategy's OOS daily return sequence N times to build a
distribution of equity paths.  A strategy whose good performance depended
on a specific lucky ordering of trades shows terrible worst-case drawdown
outcomes under reshuffling; a robust strategy sees consistent drawdown
regardless of the order its daily returns arrive in.

Statistical note: this is a *permutation* test, not a bootstrap.  Each
reshuffle draws every observed return exactly once in a random order — it
does not sample with replacement.  Sharpe ratio and CAGR are invariant
under permutation (same mean, same std, same total product), so only
path-dependent statistics such as max drawdown vary across reshuffles.
Reporting Sharpe percentiles would always show identical values; this
module therefore reports the *drawdown distribution* across reshuffles,
which is the only meaningful output of a permutation reordering test.

Usage::

    from backtesting.validation.bootstrap_stress import bootstrap_stress

    result = bootstrap_stress(wf_result.oos_returns, n_reshuffles=500, seed=42)
    print(result.verdict)            # "solid" or "fragile"
    print(result.worst_case_drawdown)
    print(result.drawdown_p5)        # near-worst: 95% of reshuffles did better
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

_TRADING_DAYS_PER_YEAR = 252


@dataclass
class BootstrapStressResult:
    """Distribution of max drawdowns from reshuffling OOS daily returns.

    All drawdown fields are negative (or zero).  Lower (more negative) values
    are worse.  The p5 field represents the near-worst tail: 95% of reshuffled
    paths had a max drawdown less severe than drawdown_p5.

    Attributes:
        n_reshuffles: Number of random reshuffles performed.
        drawdown_p5: 5th-percentile max drawdown across reshuffles (near-worst).
        drawdown_p50: Median max drawdown across reshuffles.
        drawdown_p95: 95th-percentile max drawdown across reshuffles (near-best).
        worst_case_drawdown: Minimum (most negative) max drawdown across all
            reshuffles.  Equal to or worse than drawdown_p5.
        fragile: True if worst_case_drawdown is below the fragile threshold.
        verdict: "solid" or "fragile".
    """

    n_reshuffles: int
    drawdown_p5: float
    drawdown_p50: float
    drawdown_p95: float
    worst_case_drawdown: float
    fragile: bool
    verdict: str


def bootstrap_stress(
    oos_returns: pd.Series,
    n_reshuffles: int = 500,
    fragile_drawdown_threshold: float = -0.35,
    seed: Optional[int] = None,
) -> BootstrapStressResult:
    """Stress-test a strategy by reshuffling its OOS daily return sequence.

    Each reshuffle reorders the observed daily returns randomly (permutation,
    not sampling with replacement).  Because Sharpe and CAGR are permutation-
    invariant, only drawdown varies across reshuffles.  The resulting drawdown
    distribution answers: "if this strategy's returns had arrived in a
    different order, how bad could the ride have been?"

    Args:
        oos_returns: Daily return series from the concatenated OOS walk-forward
            folds (WalkForwardResult.oos_returns).  NaN values are dropped.
        n_reshuffles: Number of random reshuffles.  Default 500.
        fragile_drawdown_threshold: Strategies whose worst-case drawdown falls
            below this value are flagged fragile.  Default -0.35 (-35%).
        seed: Optional integer seed for reproducibility.

    Returns:
        BootstrapStressResult with the drawdown distribution across reshuffles
        and a solid/fragile verdict.

    Raises:
        ValueError: If oos_returns has fewer than 2 non-NaN observations.
    """
    returns_arr = oos_returns.dropna().to_numpy(dtype=float)
    if len(returns_arr) < 2:
        raise ValueError(
            f"oos_returns must have at least 2 non-NaN observations; "
            f"got {len(returns_arr)}."
        )
    if np.any(returns_arr <= -1.0):
        raise ValueError(
            "oos_returns contains a value <= -1.0 (total portfolio loss in a single day). "
            "_max_drawdown assumes all daily returns are > -1.0."
        )

    rng = np.random.default_rng(seed)
    drawdowns: list[float] = []

    for _ in range(n_reshuffles):
        shuffled = rng.permutation(returns_arr)
        drawdowns.append(_max_drawdown(shuffled))

    drawdowns_arr = np.asarray(drawdowns, dtype=float)

    worst_dd = float(np.nanmin(drawdowns_arr))
    fragile = worst_dd < fragile_drawdown_threshold

    return BootstrapStressResult(
        n_reshuffles=n_reshuffles,
        drawdown_p5=float(np.nanpercentile(drawdowns_arr, 5)),
        drawdown_p50=float(np.nanpercentile(drawdowns_arr, 50)),
        drawdown_p95=float(np.nanpercentile(drawdowns_arr, 95)),
        worst_case_drawdown=worst_dd,
        fragile=fragile,
        verdict="fragile" if fragile else "solid",
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _annualised_sharpe(returns: np.ndarray) -> float:
    """Annualised Sharpe ratio.  Kept for external callers; note that this
    value is invariant under permutation of the input array."""
    std = returns.std(ddof=1)
    if std <= 0 or not math.isfinite(std):
        return float("nan")
    return float(returns.mean() / std * math.sqrt(_TRADING_DAYS_PER_YEAR))


def _max_drawdown(returns: np.ndarray) -> float:
    """Maximum drawdown of a return sequence.  Assumes all returns > -1.0.

    The initial equity is treated as 1.0 (the peak before any returns arrive),
    so a path that opens with losses correctly captures the drawdown from
    starting capital — e.g. [-0.10, -0.10] reports ≈ -19%, not -10%.
    """
    cumulative = np.cumprod(1.0 + returns)
    # Anchor the running peak at 1.0 (starting capital) before any compounding.
    rolling_max = np.maximum(np.maximum.accumulate(cumulative), 1.0)
    dd = cumulative / rolling_max - 1.0
    return float(dd.min())

"""Bootstrap stress test for surviving strategies.

Reshuffles a strategy's OOS daily return sequence N times to build a
distribution of equity paths.  A strategy whose good performance depended
on a specific lucky ordering of trades will show a wide spread or terrible
worst-case outcomes; a robust strategy looks similar regardless of order.

Usage::

    from backtesting.validation.bootstrap_stress import bootstrap_stress

    result = bootstrap_stress(wf_result.oos_returns, n_reshuffles=500, seed=42)
    print(result.verdict)        # "solid" or "fragile"
    print(result.worst_case_drawdown)
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
    """Distribution of outcomes from reshuffling OOS daily returns.

    Attributes:
        n_reshuffles: Number of random reshuffles performed.
        sharpe_p5: 5th-percentile annualised Sharpe across reshuffles.
        sharpe_p50: Median annualised Sharpe across reshuffles.
        sharpe_p95: 95th-percentile annualised Sharpe across reshuffles.
        worst_case_drawdown: Most-negative max drawdown across all reshuffles.
        fragile: True if worst_case_drawdown is below the fragile threshold.
        verdict: "solid" or "fragile".
    """

    n_reshuffles: int
    sharpe_p5: float
    sharpe_p50: float
    sharpe_p95: float
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

    Args:
        oos_returns: Daily return series from the concatenated OOS walk-forward
            folds (WalkForwardResult.oos_returns).  NaN values are dropped.
        n_reshuffles: Number of random reshuffles.  Default 500.
        fragile_drawdown_threshold: Strategies whose worst-case drawdown falls
            below this value are flagged fragile.  Default -0.35 (-35%).
        seed: Optional integer seed for reproducibility.

    Returns:
        BootstrapStressResult with percentile Sharpes and worst-case drawdown.

    Raises:
        ValueError: If oos_returns has fewer than 2 non-NaN observations.
    """
    returns_arr = oos_returns.dropna().to_numpy(dtype=float)
    if len(returns_arr) < 2:
        raise ValueError(
            f"oos_returns must have at least 2 non-NaN observations; "
            f"got {len(returns_arr)}."
        )

    rng = np.random.default_rng(seed)
    sharpes: list[float] = []
    drawdowns: list[float] = []

    for _ in range(n_reshuffles):
        shuffled = rng.permutation(returns_arr)
        sharpes.append(_annualised_sharpe(shuffled))
        drawdowns.append(_max_drawdown(shuffled))

    sharpes_arr = np.asarray(sharpes, dtype=float)
    drawdowns_arr = np.asarray(drawdowns, dtype=float)

    worst_dd = float(np.nanmin(drawdowns_arr))
    fragile = worst_dd < fragile_drawdown_threshold

    return BootstrapStressResult(
        n_reshuffles=n_reshuffles,
        sharpe_p5=float(np.nanpercentile(sharpes_arr, 5)),
        sharpe_p50=float(np.nanpercentile(sharpes_arr, 50)),
        sharpe_p95=float(np.nanpercentile(sharpes_arr, 95)),
        worst_case_drawdown=worst_dd,
        fragile=fragile,
        verdict="fragile" if fragile else "solid",
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _annualised_sharpe(returns: np.ndarray) -> float:
    std = returns.std(ddof=1)
    if std <= 0 or not math.isfinite(std):
        return float("nan")
    return float(returns.mean() / std * math.sqrt(_TRADING_DAYS_PER_YEAR))


def _max_drawdown(returns: np.ndarray) -> float:
    cumulative = np.cumprod(1.0 + returns)
    rolling_max = np.maximum.accumulate(cumulative)
    dd = cumulative / rolling_max - 1.0
    return float(dd.min())

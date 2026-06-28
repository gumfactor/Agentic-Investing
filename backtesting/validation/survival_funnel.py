"""Survival funnel: formal pass/fail validation gate for strategy backtests.

A strategy must clear all configured gates before it can be promoted to
VALIDATED status in the Strategy Registry.  Gate thresholds are configurable
so operators can tighten or relax criteria per use-case.

Usage::

    from backtesting.validation.survival_funnel import SurvivalFunnel
    from backtesting.validation.survival_funnel import (
        avg_is_sharpe_from_wf,
        oos_trade_count_from_wf,
    )

    funnel = SurvivalFunnel()
    result = funnel.check(
        oos_metrics=wf_result.oos_metrics,
        avg_is_sharpe=avg_is_sharpe_from_wf(wf_result),
        trade_count=oos_trade_count_from_wf(wf_result),
    )
    print(result.verdict)   # "PASS — ..." or "FAIL — gates not cleared: ..."
    print(result.passed)    # True / False
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import structlog

if TYPE_CHECKING:
    from backtesting.validation.walk_forward import WalkForwardResult

logger = structlog.get_logger(__name__)


@dataclass
class FunnelGate:
    """Result for one validation gate.

    Attributes:
        name: Machine-readable gate identifier.
        passed: True if the gate was cleared.
        value: Observed metric value.
        threshold: The gate threshold.
        description: Human-readable description of what the gate checks.
    """

    name: str
    passed: bool
    value: float
    threshold: float
    description: str


@dataclass
class SurvivalFunnelResult:
    """Aggregate result from running all funnel gates.

    Attributes:
        passed: True only if every gate passed.
        gates: Per-gate results in evaluation order.
        verdict: Human-readable summary string.
    """

    passed: bool
    gates: list[FunnelGate]
    verdict: str


class SurvivalFunnel:
    """Configurable pass/fail validation gate for walk-forward backtest results.

    Gates (all thresholds configurable):

    1. min_oos_sharpe   — OOS Sharpe must exceed the floor (default 0.5).
    2. max_oos_drawdown — OOS max drawdown must not be too severe (default -35%).
    3. max_oos_sharpe   — OOS Sharpe must not be suspiciously high, which
                          indicates a lucky OOS period rather than real edge
                          (default < 2.5).
    4. is_oos_consistency — The relative gap between average IS Sharpe and OOS
                            Sharpe must be within tolerance.  A large gap in
                            either direction signals either overfitting (IS ≫ OOS)
                            or a lucky OOS draw (OOS ≫ IS).  Default 30%.
    5. min_trade_count  — At least this many OOS trades are required for the
                          result to be statistically meaningful (default 30).
    6. positive_is_sharpe — Average IS Sharpe must be positive; a strategy that
                            lost money in training has no business being validated.

    Args:
        min_oos_sharpe: Gate 1 threshold.  Default 0.5.
        max_oos_drawdown: Gate 2 threshold (negative).  Default -0.35.
        max_oos_sharpe: Gate 3 threshold.  Default 2.5.
        max_is_oos_gap: Gate 4 threshold as a fraction of IS Sharpe.  Default 0.30.
        min_trade_count: Gate 5 threshold.  Default 30.
    """

    def __init__(
        self,
        min_oos_sharpe: float = 0.5,
        max_oos_drawdown: float = -0.35,
        max_oos_sharpe: float = 2.5,
        max_is_oos_gap: float = 0.30,
        min_trade_count: int = 30,
    ) -> None:
        self._min_oos_sharpe = min_oos_sharpe
        self._max_oos_drawdown = max_oos_drawdown
        self._max_oos_sharpe = max_oos_sharpe
        self._max_is_oos_gap = max_is_oos_gap
        self._min_trade_count = min_trade_count

    def check(
        self,
        oos_metrics: dict,
        avg_is_sharpe: float,
        trade_count: int,
    ) -> SurvivalFunnelResult:
        """Evaluate a strategy against all gates.

        Args:
            oos_metrics: Metrics dict from WalkForwardResult.oos_metrics.
                Must contain at minimum 'sharpe' and 'max_drawdown'.
            avg_is_sharpe: Average in-sample Sharpe across walk-forward folds.
                Use avg_is_sharpe_from_wf() to compute this from a WalkForwardResult.
            trade_count: Total OOS trade count across all folds.
                Use oos_trade_count_from_wf() to compute this.

        Returns:
            SurvivalFunnelResult with per-gate verdicts and overall pass/fail.
        """
        oos_sharpe = _f(oos_metrics.get("sharpe"))
        oos_dd = _f(oos_metrics.get("max_drawdown"))
        is_sharpe = _f(avg_is_sharpe)

        gates: list[FunnelGate] = [
            self._gate_min_oos_sharpe(oos_sharpe),
            self._gate_max_oos_drawdown(oos_dd),
            self._gate_max_oos_sharpe(oos_sharpe),
            self._gate_is_oos_consistency(is_sharpe, oos_sharpe),
            self._gate_min_trade_count(trade_count),
            self._gate_positive_is_sharpe(is_sharpe),
        ]

        passed = all(g.passed for g in gates)
        failed = [g.name for g in gates if not g.passed]
        verdict = (
            "PASS — strategy cleared all validation gates"
            if passed
            else f"FAIL — gates not cleared: {', '.join(failed)}"
        )
        if not passed:
            logger.warning(
                "survival_funnel_failed",
                failed_gates=failed,
                oos_sharpe=oos_sharpe,
                oos_max_drawdown=oos_dd,
                avg_is_sharpe=is_sharpe,
                trade_count=trade_count,
            )
        return SurvivalFunnelResult(passed=passed, gates=gates, verdict=verdict)

    # ------------------------------------------------------------------
    # Individual gate builders
    # ------------------------------------------------------------------

    def _gate_min_oos_sharpe(self, oos_sharpe: float) -> FunnelGate:
        return FunnelGate(
            name="min_oos_sharpe",
            passed=_ok(oos_sharpe) and oos_sharpe >= self._min_oos_sharpe,
            value=oos_sharpe,
            threshold=self._min_oos_sharpe,
            description=f"OOS Sharpe >= {self._min_oos_sharpe}",
        )

    def _gate_max_oos_drawdown(self, oos_dd: float) -> FunnelGate:
        return FunnelGate(
            name="max_oos_drawdown",
            passed=_ok(oos_dd) and oos_dd >= self._max_oos_drawdown,
            value=oos_dd,
            threshold=self._max_oos_drawdown,
            description=f"OOS max drawdown >= {self._max_oos_drawdown:.0%}",
        )

    def _gate_max_oos_sharpe(self, oos_sharpe: float) -> FunnelGate:
        return FunnelGate(
            name="max_oos_sharpe",
            passed=_ok(oos_sharpe) and oos_sharpe < self._max_oos_sharpe,
            value=oos_sharpe,
            threshold=self._max_oos_sharpe,
            description=f"OOS Sharpe < {self._max_oos_sharpe} (lucky-artifact check)",
        )

    def _gate_is_oos_consistency(self, is_sharpe: float, oos_sharpe: float) -> FunnelGate:
        if _ok(is_sharpe) and _ok(oos_sharpe) and abs(is_sharpe) > 1e-6:
            gap = abs(is_sharpe - oos_sharpe) / abs(is_sharpe)
        else:
            gap = float("nan")
        return FunnelGate(
            name="is_oos_consistency",
            passed=_ok(gap) and gap <= self._max_is_oos_gap,
            value=gap,
            threshold=self._max_is_oos_gap,
            description=f"|IS - OOS| / |IS| <= {self._max_is_oos_gap:.0%}",
        )

    def _gate_min_trade_count(self, trade_count: int) -> FunnelGate:
        return FunnelGate(
            name="min_trade_count",
            passed=trade_count >= self._min_trade_count,
            value=float(trade_count),
            threshold=float(self._min_trade_count),
            description=f"OOS trade count >= {self._min_trade_count}",
        )

    def _gate_positive_is_sharpe(self, is_sharpe: float) -> FunnelGate:
        # Threshold matches the dead zone in _gate_is_oos_consistency (abs(is) > 1e-6).
        # A near-zero IS Sharpe would make the consistency gap ratio undefined;
        # requiring it to exceed 1e-6 keeps both gates consistent.
        return FunnelGate(
            name="positive_is_sharpe",
            passed=_ok(is_sharpe) and is_sharpe > 1e-6,
            value=is_sharpe,
            threshold=1e-6,
            description="Average IS Sharpe > 0 (above numerical dead zone)",
        )


# ------------------------------------------------------------------
# Walk-forward convenience helpers
# ------------------------------------------------------------------

def avg_is_sharpe_from_wf(wf_result: "WalkForwardResult") -> float:
    """Compute average in-sample Sharpe across all folds of a WalkForwardResult."""
    sharpes = [
        f.in_sample.metrics.get("sharpe", float("nan"))
        for f in wf_result.folds
    ]
    return float(np.nanmean(sharpes)) if sharpes else float("nan")


def oos_trade_count_from_wf(wf_result: "WalkForwardResult") -> int:
    """Count total OOS trades across all folds of a WalkForwardResult."""
    dfs = [f.out_of_sample.trades for f in wf_result.folds if not f.out_of_sample.trades.empty]
    if not dfs:
        return 0
    return len(pd.concat(dfs, ignore_index=True))


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _f(v) -> float:
    """Cast to float; None becomes NaN."""
    if v is None:
        return float("nan")
    return float(v)


def _ok(v: float) -> bool:
    """True if v is a finite, non-NaN float."""
    return math.isfinite(v)

"""Tests for SurvivalFunnel and helper functions."""
from __future__ import annotations

import math
from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from backtesting.validation.survival_funnel import (
    FunnelGate,
    SurvivalFunnel,
    SurvivalFunnelResult,
    avg_is_sharpe_from_wf,
    oos_trade_count_from_wf,
    _f,
    _ok,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

def _good_oos_metrics() -> dict:
    return {"sharpe": 0.80, "max_drawdown": -0.18}


def _make_funnel(**kwargs) -> SurvivalFunnel:
    return SurvivalFunnel(**kwargs)


def _check(
    oos_metrics: dict | None = None,
    avg_is_sharpe: float = 0.90,
    trade_count: int = 50,
    **funnel_kwargs,
) -> SurvivalFunnelResult:
    funnel = _make_funnel(**funnel_kwargs)
    return funnel.check(
        oos_metrics=oos_metrics or _good_oos_metrics(),
        avg_is_sharpe=avg_is_sharpe,
        trade_count=trade_count,
    )


# ------------------------------------------------------------------
# Happy-path: all gates pass
# ------------------------------------------------------------------

def test_all_gates_pass_for_good_strategy():
    result = _check()
    assert result.passed is True
    assert all(g.passed for g in result.gates)
    assert "PASS" in result.verdict


def test_result_has_six_gates():
    result = _check()
    assert len(result.gates) == 6


def test_gate_names_present():
    result = _check()
    names = {g.name for g in result.gates}
    assert "min_oos_sharpe" in names
    assert "max_oos_drawdown" in names
    assert "max_oos_sharpe" in names
    assert "is_oos_consistency" in names
    assert "min_trade_count" in names
    assert "positive_is_sharpe" in names


# ------------------------------------------------------------------
# Gate 1: min_oos_sharpe
# ------------------------------------------------------------------

def test_low_oos_sharpe_fails():
    result = _check(oos_metrics={"sharpe": 0.3, "max_drawdown": -0.10})
    gate = next(g for g in result.gates if g.name == "min_oos_sharpe")
    assert gate.passed is False
    assert result.passed is False
    assert "FAIL" in result.verdict


def test_oos_sharpe_exactly_at_threshold_passes():
    result = _check(oos_metrics={"sharpe": 0.5, "max_drawdown": -0.10})
    gate = next(g for g in result.gates if g.name == "min_oos_sharpe")
    assert gate.passed is True


def test_nan_oos_sharpe_fails():
    result = _check(oos_metrics={"sharpe": float("nan"), "max_drawdown": -0.10})
    gate = next(g for g in result.gates if g.name == "min_oos_sharpe")
    assert gate.passed is False


# ------------------------------------------------------------------
# Gate 2: max_oos_drawdown
# ------------------------------------------------------------------

def test_severe_drawdown_fails():
    result = _check(oos_metrics={"sharpe": 0.80, "max_drawdown": -0.40})
    gate = next(g for g in result.gates if g.name == "max_oos_drawdown")
    assert gate.passed is False
    assert result.passed is False


def test_drawdown_exactly_at_threshold_passes():
    result = _check(oos_metrics={"sharpe": 0.80, "max_drawdown": -0.35})
    gate = next(g for g in result.gates if g.name == "max_oos_drawdown")
    assert gate.passed is True


# ------------------------------------------------------------------
# Gate 3: max_oos_sharpe (lucky-artifact check)
# ------------------------------------------------------------------

def test_suspiciously_high_oos_sharpe_fails():
    result = _check(oos_metrics={"sharpe": 3.0, "max_drawdown": -0.05})
    gate = next(g for g in result.gates if g.name == "max_oos_sharpe")
    assert gate.passed is False
    assert result.passed is False


def test_sharpe_just_below_ceiling_passes():
    result = _check(oos_metrics={"sharpe": 2.49, "max_drawdown": -0.05}, avg_is_sharpe=2.3)
    gate = next(g for g in result.gates if g.name == "max_oos_sharpe")
    assert gate.passed is True


def test_custom_max_oos_sharpe_threshold():
    funnel = SurvivalFunnel(max_oos_sharpe=1.5)
    result = funnel.check(
        oos_metrics={"sharpe": 1.6, "max_drawdown": -0.10},
        avg_is_sharpe=1.5,
        trade_count=50,
    )
    gate = next(g for g in result.gates if g.name == "max_oos_sharpe")
    assert gate.passed is False


# ------------------------------------------------------------------
# Gate 4: IS/OOS consistency
# ------------------------------------------------------------------

def test_large_is_oos_gap_fails():
    # IS Sharpe = 2.0, OOS Sharpe = 0.8 → gap = 60% > 30%
    result = _check(
        oos_metrics={"sharpe": 0.8, "max_drawdown": -0.15},
        avg_is_sharpe=2.0,
    )
    gate = next(g for g in result.gates if g.name == "is_oos_consistency")
    assert gate.passed is False


def test_consistent_is_oos_passes():
    # IS Sharpe = 0.90, OOS Sharpe = 0.80 → gap ≈ 11%
    result = _check(
        oos_metrics={"sharpe": 0.80, "max_drawdown": -0.15},
        avg_is_sharpe=0.90,
    )
    gate = next(g for g in result.gates if g.name == "is_oos_consistency")
    assert gate.passed is True


def test_nan_is_sharpe_fails_consistency_gate():
    result = _check(avg_is_sharpe=float("nan"))
    gate = next(g for g in result.gates if g.name == "is_oos_consistency")
    assert gate.passed is False


# ------------------------------------------------------------------
# Gate 5: min_trade_count
# ------------------------------------------------------------------

def test_too_few_trades_fails():
    result = _check(trade_count=10)
    gate = next(g for g in result.gates if g.name == "min_trade_count")
    assert gate.passed is False
    assert result.passed is False


def test_exactly_min_trades_passes():
    result = _check(trade_count=30)
    gate = next(g for g in result.gates if g.name == "min_trade_count")
    assert gate.passed is True


# ------------------------------------------------------------------
# Gate 6: positive_is_sharpe
# ------------------------------------------------------------------

def test_negative_is_sharpe_fails():
    result = _check(avg_is_sharpe=-0.10)
    gate = next(g for g in result.gates if g.name == "positive_is_sharpe")
    assert gate.passed is False
    assert result.passed is False


def test_zero_is_sharpe_fails():
    result = _check(avg_is_sharpe=0.0)
    gate = next(g for g in result.gates if g.name == "positive_is_sharpe")
    assert gate.passed is False


def test_dead_zone_is_sharpe_fails_both_gates():
    """IS Sharpe in (0, 1e-6] must fail gate 6 (positive_is_sharpe) AND gate 4
    (is_oos_consistency) consistently — the two gates use the same 1e-6 dead zone."""
    dead_zone_sharpe = 5e-7   # positive but below 1e-6
    result = _check(
        avg_is_sharpe=dead_zone_sharpe,
        oos_metrics={"sharpe": 0.80, "max_drawdown": -0.15},
    )
    gate_pos = next(g for g in result.gates if g.name == "positive_is_sharpe")
    gate_con = next(g for g in result.gates if g.name == "is_oos_consistency")
    # Gate 6 must fail — IS Sharpe is below the 1e-6 threshold
    assert gate_pos.passed is False
    # Gate 4 also fails — IS Sharpe triggers the dead-zone guard (NaN gap)
    assert gate_con.passed is False


# ------------------------------------------------------------------
# Configurable thresholds
# ------------------------------------------------------------------

def test_custom_min_sharpe_threshold():
    # Strategy with OOS Sharpe 0.7 should fail min=0.8 but pass min=0.6
    strict = SurvivalFunnel(min_oos_sharpe=0.8)
    lenient = SurvivalFunnel(min_oos_sharpe=0.6)
    metrics = {"sharpe": 0.7, "max_drawdown": -0.15}
    assert strict.check(metrics, 0.75, 50).gates[0].passed is False
    assert lenient.check(metrics, 0.75, 50).gates[0].passed is True


def test_failed_gate_names_in_verdict():
    result = _check(trade_count=5)
    assert "min_trade_count" in result.verdict


# ------------------------------------------------------------------
# Walk-forward helper functions
# ------------------------------------------------------------------

def _make_mock_fold(is_sharpe: float, n_trades: int):
    fold = MagicMock()
    fold.in_sample.metrics = {"sharpe": is_sharpe}
    if n_trades > 0:
        fold.out_of_sample.trades = pd.DataFrame(
            {"ticker": ["A"] * n_trades, "direction": ["BUY"] * n_trades}
        )
    else:
        fold.out_of_sample.trades = pd.DataFrame()
    return fold


def _make_mock_wf(fold_sharpes: list[float], trades_per_fold: list[int]):
    wf = MagicMock()
    wf.folds = [
        _make_mock_fold(s, t)
        for s, t in zip(fold_sharpes, trades_per_fold)
    ]
    return wf


def test_avg_is_sharpe_from_wf_basic():
    wf = _make_mock_wf([0.8, 1.0, 0.6], [10, 10, 10])
    result = avg_is_sharpe_from_wf(wf)
    assert result == pytest.approx(0.8, rel=1e-6)


def test_avg_is_sharpe_from_wf_with_nan():
    wf = _make_mock_wf([float("nan"), 1.0, 0.6], [10, 10, 10])
    result = avg_is_sharpe_from_wf(wf)
    assert result == pytest.approx(0.8, rel=1e-6)


def test_avg_is_sharpe_from_wf_empty_folds():
    wf = MagicMock()
    wf.folds = []
    result = avg_is_sharpe_from_wf(wf)
    assert not math.isfinite(result)


def test_oos_trade_count_from_wf():
    wf = _make_mock_wf([0.8, 0.9, 0.7], [15, 20, 10])
    assert oos_trade_count_from_wf(wf) == 45


def test_oos_trade_count_from_wf_no_trades():
    wf = _make_mock_wf([0.8, 0.9], [0, 0])
    assert oos_trade_count_from_wf(wf) == 0


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def test_f_handles_none():
    assert not math.isfinite(_f(None))


def test_f_handles_float():
    assert _f(1.5) == 1.5


def test_ok_true_for_finite():
    assert _ok(1.0) is True


def test_ok_false_for_nan():
    assert _ok(float("nan")) is False


def test_ok_false_for_inf():
    assert _ok(float("inf")) is False

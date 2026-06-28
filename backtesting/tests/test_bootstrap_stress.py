"""Tests for bootstrap_stress."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backtesting.validation.bootstrap_stress import (
    BootstrapStressResult,
    bootstrap_stress,
    _annualised_sharpe,
    _max_drawdown,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

def _flat_returns(n: int = 252, daily_ret: float = 0.001) -> pd.Series:
    """Uniform positive daily returns."""
    return pd.Series([daily_ret] * n)


def _volatile_returns(n: int = 252, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0.0, 0.03, size=n))


# ------------------------------------------------------------------
# bootstrap_stress: basic structure
# ------------------------------------------------------------------

def test_returns_correct_type():
    result = bootstrap_stress(_flat_returns(), n_reshuffles=50, seed=0)
    assert isinstance(result, BootstrapStressResult)


def test_n_reshuffles_recorded():
    result = bootstrap_stress(_flat_returns(), n_reshuffles=100, seed=0)
    assert result.n_reshuffles == 100


def test_drawdown_percentile_ordering():
    result = bootstrap_stress(_volatile_returns(), n_reshuffles=200, seed=42)
    # All drawdowns are <= 0; p5 is worst (most negative), p95 is best (least negative)
    assert result.drawdown_p5 <= result.drawdown_p50 <= result.drawdown_p95


def test_drawdown_percentiles_are_nonpositive():
    result = bootstrap_stress(_volatile_returns(), n_reshuffles=100, seed=0)
    assert result.drawdown_p5 <= 0.0
    assert result.drawdown_p50 <= 0.0
    assert result.drawdown_p95 <= 0.0


def test_drawdown_varies_across_reshuffles():
    """Max drawdown is path-dependent, so different reshuffles should produce different values."""
    returns = _volatile_returns(n=252, seed=7)
    result = bootstrap_stress(returns, n_reshuffles=200, seed=42)
    # If drawdown were permutation-invariant, p5 == p95; verify they differ
    assert result.drawdown_p5 < result.drawdown_p95


def test_worst_case_drawdown_is_nonpositive():
    result = bootstrap_stress(_volatile_returns(), n_reshuffles=50, seed=1)
    assert result.worst_case_drawdown <= 0.0


def test_verdict_values():
    result = bootstrap_stress(_volatile_returns(), n_reshuffles=50, seed=1)
    assert result.verdict in ("solid", "fragile")


# ------------------------------------------------------------------
# Solid vs fragile classification
# ------------------------------------------------------------------

def test_flat_positive_returns_are_solid():
    """Uniform positive returns can never produce a severe drawdown on any reshuffle."""
    result = bootstrap_stress(_flat_returns(252, 0.001), n_reshuffles=100, seed=0)
    assert result.fragile is False
    assert result.verdict == "solid"


def test_severe_crash_series_is_fragile():
    """Returns that crash 50% are always flagged fragile regardless of order."""
    # half the days are -0.01, producing a deep cumulative drawdown over any reorder
    crash_returns = pd.Series([-0.01] * 126 + [0.005] * 126)
    result = bootstrap_stress(crash_returns, n_reshuffles=100, seed=0)
    assert result.fragile is True
    assert result.worst_case_drawdown < -0.35


def test_custom_threshold_changes_classification():
    """A lenient threshold should make the same series pass as solid."""
    crash_returns = pd.Series([-0.005] * 100 + [0.003] * 152)
    strict = bootstrap_stress(crash_returns, fragile_drawdown_threshold=-0.05, n_reshuffles=50, seed=0)
    lenient = bootstrap_stress(crash_returns, fragile_drawdown_threshold=-0.99, n_reshuffles=50, seed=0)
    assert lenient.fragile is False
    # strict should flag as fragile
    assert strict.fragile is True


# ------------------------------------------------------------------
# Reproducibility
# ------------------------------------------------------------------

def test_seed_reproducibility():
    returns = _volatile_returns()
    r1 = bootstrap_stress(returns, n_reshuffles=100, seed=99)
    r2 = bootstrap_stress(returns, n_reshuffles=100, seed=99)
    assert r1.drawdown_p50 == r2.drawdown_p50
    assert r1.worst_case_drawdown == r2.worst_case_drawdown


def test_different_seeds_differ():
    returns = _volatile_returns()
    r1 = bootstrap_stress(returns, n_reshuffles=200, seed=1)
    r2 = bootstrap_stress(returns, n_reshuffles=200, seed=2)
    assert r1.drawdown_p50 != r2.drawdown_p50 or r1.worst_case_drawdown != r2.worst_case_drawdown


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------

def test_empty_series_raises():
    with pytest.raises(ValueError, match="at least 2"):
        bootstrap_stress(pd.Series([], dtype=float))


def test_single_observation_raises():
    with pytest.raises(ValueError, match="at least 2"):
        bootstrap_stress(pd.Series([0.01]))


def test_nan_values_are_dropped():
    returns = pd.Series([0.01, float("nan"), 0.02, float("nan"), 0.005])
    result = bootstrap_stress(returns, n_reshuffles=50, seed=0)
    assert isinstance(result, BootstrapStressResult)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def test_annualised_sharpe_positive_returns():
    arr = np.full(252, 0.001)
    sr = _annualised_sharpe(arr)
    assert math.isfinite(sr)
    assert sr > 0


def test_annualised_sharpe_zero_std():
    arr = np.full(10, 0.0)   # all zeros → std = 0
    sr = _annualised_sharpe(arr)
    assert not math.isfinite(sr)  # nan or inf


def test_max_drawdown_all_positive():
    arr = np.array([0.01] * 10)
    dd = _max_drawdown(arr)
    assert dd == pytest.approx(0.0, abs=1e-9)


def test_max_drawdown_with_drop():
    arr = np.array([0.1, -0.2, 0.1])
    dd = _max_drawdown(arr)
    assert dd < 0.0

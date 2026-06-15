"""Tests for portfolio optimizers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from portfolio.optimization.mvo import MVOOptimizer
from portfolio.optimization.risk_parity import RiskParityOptimizer
from portfolio.risk_model.constraints import PortfolioConstraints


# ── Fixtures ──────────────────────────────────────────────────────────────────

TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]


@pytest.fixture
def expected_returns() -> pd.Series:
    return pd.Series(
        [0.12, 0.10, 0.09, 0.11, 0.15],
        index=TICKERS,
    )


@pytest.fixture
def covariance() -> pd.DataFrame:
    """Simple diagonal covariance (independent assets) for deterministic tests."""
    vols = np.array([0.20, 0.18, 0.22, 0.25, 0.30])
    cov = np.diag(vols ** 2)
    return pd.DataFrame(cov, index=TICKERS, columns=TICKERS)


@pytest.fixture
def constraints() -> PortfolioConstraints:
    return PortfolioConstraints(max_position_weight=0.40)


# ── MVO tests ─────────────────────────────────────────────────────────────────

class TestMVOOptimizer:
    def test_max_sharpe_weights_sum_to_one(self, expected_returns, covariance, constraints):
        optimizer = MVOOptimizer(mode="max_sharpe")
        result = optimizer.run(expected_returns, covariance, constraints)
        assert abs(result.weights.sum() - 1.0) < 1e-4

    def test_max_sharpe_weights_non_negative(self, expected_returns, covariance, constraints):
        optimizer = MVOOptimizer(mode="max_sharpe")
        result = optimizer.run(expected_returns, covariance, constraints)
        assert (result.weights >= -1e-6).all()

    def test_max_sharpe_respects_position_cap(self, expected_returns, covariance):
        cap = 0.30
        c = PortfolioConstraints(max_position_weight=cap)
        optimizer = MVOOptimizer(mode="max_sharpe")
        result = optimizer.run(expected_returns, covariance, c)
        assert (result.weights <= cap + 1e-4).all()

    def test_min_variance_mode(self, expected_returns, covariance, constraints):
        optimizer = MVOOptimizer(mode="min_variance")
        result = optimizer.run(expected_returns, covariance, constraints)
        assert abs(result.weights.sum() - 1.0) < 1e-4
        # Min-variance should allocate most to lowest-vol asset (MSFT, vol=18%)
        assert result.weights["MSFT"] > result.weights["NVDA"]

    def test_mean_variance_mode(self, expected_returns, covariance, constraints):
        optimizer = MVOOptimizer(mode="mean_variance", risk_aversion=2.0)
        result = optimizer.run(expected_returns, covariance, constraints)
        assert abs(result.weights.sum() - 1.0) < 1e-4

    def test_sector_constraint(self, expected_returns, covariance):
        # Put first 3 tickers in "Tech" and last 2 in "Energy".
        # Cap "Tech" at 60% — the optimizer must constrain Tech total ≤ 0.60.
        sector_map = {
            "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech",
            "AMZN": "Energy", "NVDA": "Energy",
        }
        sector_cap = 0.60
        c = PortfolioConstraints(
            max_position_weight=0.40,
            max_sector_weight=sector_cap,
            sector_map=sector_map,
        )
        optimizer = MVOOptimizer(mode="min_variance")
        result = optimizer.run(expected_returns, covariance, c)
        assert abs(result.weights.sum() - 1.0) < 1e-4
        # Tech sector weight must be ≤ cap
        tech_weight = result.weights[["AAPL", "MSFT", "GOOGL"]].sum()
        assert tech_weight <= sector_cap + 1e-4, (
            f"Tech sector weight {tech_weight:.4f} exceeds cap {sector_cap}"
        )
        # Energy sector weight must also be ≤ cap
        energy_weight = result.weights[["AMZN", "NVDA"]].sum()
        assert energy_weight <= sector_cap + 1e-4, (
            f"Energy sector weight {energy_weight:.4f} exceeds cap {sector_cap}"
        )

    def test_mismatched_tickers_use_intersection(self, covariance):
        mu = pd.Series([0.10, 0.12], index=["AAPL", "EXTRA"])
        optimizer = MVOOptimizer()
        result = optimizer.run(mu, covariance)
        # Only AAPL is in both; single-asset portfolio = weight 1.0
        assert "AAPL" in result.weights.index

    def test_empty_intersection_raises(self, covariance):
        mu = pd.Series([0.10], index=["ZZZ"])
        optimizer = MVOOptimizer()
        with pytest.raises(ValueError, match="No common tickers"):
            optimizer.run(mu, covariance)


# ── Risk Parity tests ─────────────────────────────────────────────────────────

class TestRiskParityOptimizer:
    def test_weights_sum_to_one(self, expected_returns, covariance, constraints):
        optimizer = RiskParityOptimizer()
        result = optimizer.run(expected_returns, covariance, constraints)
        assert abs(result.weights.sum() - 1.0) < 1e-4

    def test_weights_non_negative(self, expected_returns, covariance, constraints):
        optimizer = RiskParityOptimizer()
        result = optimizer.run(expected_returns, covariance, constraints)
        assert (result.weights >= -1e-6).all()

    def test_equal_risk_contributions_diagonal_cov(self, expected_returns, covariance):
        """With diagonal cov, equal-risk = inverse-vol weighting (no position cap)."""
        c = PortfolioConstraints(max_position_weight=1.0)  # no cap; test pure ERC property
        optimizer = RiskParityOptimizer()
        result = optimizer.run(expected_returns, covariance, c)
        w = result.weights.values
        # Align covariance to the (alphabetically sorted) weight index
        Sigma = covariance.loc[result.weights.index, result.weights.index].values
        pvar = float(w @ Sigma @ w)
        rc = w * (Sigma @ w) / pvar
        # All risk contributions should be approximately equal
        assert np.std(rc) < 0.02, f"RC std too high: {np.std(rc):.4f}"

    def test_position_cap_respected(self, expected_returns, covariance):
        c = PortfolioConstraints(max_position_weight=0.30)
        optimizer = RiskParityOptimizer()
        result = optimizer.run(expected_returns, covariance, c)
        assert (result.weights <= 0.30 + 1e-3).all()

    def test_position_cap_respected_after_iterative_normalization(self, expected_returns, covariance):
        """Tight cap (0.25) with 5 assets — iterative normalization must not overshoot."""
        c = PortfolioConstraints(max_position_weight=0.25)
        optimizer = RiskParityOptimizer()
        result = optimizer.run(expected_returns, covariance, c)
        # All weights must be at or below the cap after iterative renormalization
        assert (result.weights <= 0.25 + 1e-6).all(), (
            f"Weight cap violated: {result.weights.max():.6f} > 0.25"
        )

    def test_custom_budget(self, expected_returns, covariance):
        n = len(TICKERS)
        budget = np.array([0.30, 0.20, 0.20, 0.20, 0.10])
        optimizer = RiskParityOptimizer(budget=budget)
        result = optimizer.run(expected_returns, covariance)
        assert abs(result.weights.sum() - 1.0) < 1e-4

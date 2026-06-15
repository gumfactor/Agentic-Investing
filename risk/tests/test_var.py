"""Tests for VaR and risk metric calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from risk.realtime.var import conditional_var, historical_var, parametric_var, portfolio_beta


@pytest.fixture
def portfolio_returns() -> pd.Series:
    rng = np.random.default_rng(0)
    returns = pd.Series(rng.normal(-0.001, 0.012, 252))
    return returns


@pytest.fixture
def weights() -> pd.Series:
    return pd.Series({"AAPL": 0.40, "MSFT": 0.35, "GOOGL": 0.25})


@pytest.fixture
def covariance() -> pd.DataFrame:
    tickers = ["AAPL", "MSFT", "GOOGL"]
    vols = np.array([0.25, 0.22, 0.28])
    cov = np.diag(vols ** 2)
    return pd.DataFrame(cov, index=tickers, columns=tickers)


class TestHistoricalVar:
    def test_returns_positive_value(self, portfolio_returns):
        var = historical_var(portfolio_returns)
        assert var >= 0

    def test_99pct_greater_than_95pct(self, portfolio_returns):
        var_99 = historical_var(portfolio_returns, confidence=0.99)
        var_95 = historical_var(portfolio_returns, confidence=0.95)
        assert var_99 >= var_95

    def test_multday_scaling(self, portfolio_returns):
        var_1d = historical_var(portfolio_returns, horizon_days=1)
        var_10d = historical_var(portfolio_returns, horizon_days=10)
        assert abs(var_10d / var_1d - np.sqrt(10)) < 0.01

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="≥ 30"):
            historical_var(pd.Series([0.01] * 10))


class TestParametricVar:
    def test_returns_positive_value(self, weights, covariance):
        var = parametric_var(weights, covariance)
        assert var > 0

    def test_scales_with_horizon(self, weights, covariance):
        var_1d = parametric_var(weights, covariance, horizon_days=1)
        var_4d = parametric_var(weights, covariance, horizon_days=4)
        assert abs(var_4d / var_1d - 2.0) < 0.01

    def test_handles_extra_tickers_in_weights(self, weights, covariance):
        w = weights.copy()
        w["XYZ"] = 0.10  # not in covariance
        var = parametric_var(w, covariance)
        assert var > 0


class TestConditionalVar:
    def test_cvar_gte_var(self, portfolio_returns):
        var = historical_var(portfolio_returns, confidence=0.95)
        cvar = conditional_var(portfolio_returns, confidence=0.95)
        assert cvar >= var - 1e-6

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="≥ 30"):
            conditional_var(pd.Series([0.01] * 10))


class TestPortfolioBeta:
    def test_beta_one_asset_tracks_benchmark(self):
        rng = np.random.default_rng(1)
        bench = pd.Series(rng.normal(0, 0.01, 100))
        # Asset = 2 * benchmark + noise → beta ≈ 2
        asset = 2 * bench + rng.normal(0, 0.002, 100)
        weights = pd.Series({"A": 1.0})
        asset_returns = pd.DataFrame({"A": asset})
        beta = portfolio_beta(weights, asset_returns, bench)
        assert abs(beta - 2.0) < 0.2

    def test_beta_empty_weights_returns_zero(self):
        beta = portfolio_beta(pd.Series(dtype=float), pd.DataFrame(), pd.Series(dtype=float))
        assert beta == 0.0

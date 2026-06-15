"""Tests for covariance estimators."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from portfolio.risk_model.covariance import build_covariance, returns_from_prices


@pytest.fixture
def daily_returns() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2023-01-01", periods=252)
    tickers = ["A", "B", "C"]
    data = rng.normal(0, 0.01, size=(252, 3))
    return pd.DataFrame(data, index=dates, columns=tickers)


class TestBuildCovariance:
    def test_sample_returns_square_symmetric(self, daily_returns):
        cov = build_covariance(daily_returns, method="sample")
        assert cov.shape == (3, 3)
        pd.testing.assert_frame_equal(cov, cov.T, check_exact=False, atol=1e-10)

    def test_ledoit_wolf_positive_definite(self, daily_returns):
        cov = build_covariance(daily_returns, method="ledoit_wolf")
        eigvals = np.linalg.eigvalsh(cov.values)
        assert eigvals.min() > 0, "Ledoit-Wolf result must be PD"

    def test_oas_method(self, daily_returns):
        cov = build_covariance(daily_returns, method="oas")
        assert cov.shape == (3, 3)

    def test_raises_insufficient_data(self, daily_returns):
        with pytest.raises(ValueError, match="Need"):
            build_covariance(daily_returns.head(50), method="sample", min_days=126)

    def test_annualization(self, daily_returns):
        cov_daily = build_covariance(daily_returns, method="sample", annualize=False)
        cov_annual = build_covariance(daily_returns, method="sample", annualize=True)
        ratio = (cov_annual.values / cov_daily.values).mean()
        assert abs(ratio - 252) < 5  # approximately 252x

    def test_unknown_method_raises(self, daily_returns):
        with pytest.raises(ValueError, match="Unknown"):
            build_covariance(daily_returns, method="bogus")


class TestReturnsFromPrices:
    def test_basic_returns(self):
        prices = pd.DataFrame(
            {"A": [100, 102, 101, 103], "B": [50, 51, 52, 50]},
            index=pd.to_datetime(["2023-01-02", "2023-01-03", "2023-01-04", "2023-01-05"]),
        )
        returns = returns_from_prices(prices, as_of=pd.Timestamp("2023-01-05").date())
        assert len(returns) == 3  # 4 prices → 3 returns
        assert "A" in returns.columns

    def test_pit_safety(self):
        prices = pd.DataFrame(
            {"A": [100, 102, 104, 106]},
            index=pd.to_datetime(["2023-01-02", "2023-01-03", "2023-01-04", "2023-01-05"]),
        )
        # as_of = 2023-01-03 → should only use first 2 prices
        returns = returns_from_prices(prices, as_of=pd.Timestamp("2023-01-03").date())
        assert len(returns) == 1

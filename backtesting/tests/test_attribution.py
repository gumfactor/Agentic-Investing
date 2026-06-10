"""Tests for Brinson attribution and factor decomposition."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from backtesting.attribution.brinson import compute_brinson_attribution
from backtesting.attribution.factor_decomposition import (
    decompose_factor_returns,
    compute_factor_contributions,
)


# ------------------------------------------------------------------
# Brinson attribution
# ------------------------------------------------------------------

def _make_weights(date_val: date, tickers: list[str], weights: list[float], sector: str = "Tech") -> pd.DataFrame:
    return pd.DataFrame({
        "date": [date_val] * len(tickers),
        "ticker": tickers,
        "weight": weights,
        "sector": [sector] * len(tickers),
    })


def _make_returns(date_val: date, tickers: list[str], rets: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": [date_val] * len(tickers),
        "ticker": tickers,
        "return": rets,
    })


def test_brinson_effects_sum_to_total():
    dt = date(2023, 1, 31)
    pw = _make_weights(dt, ["A", "B"], [0.7, 0.3], "Tech")
    bw = _make_weights(dt, ["A", "B"], [0.5, 0.5], "Tech")
    rets = _make_returns(dt, ["A", "B"], [0.05, 0.02])

    result = compute_brinson_attribution(pw, bw, rets)
    assert abs(result.total_allocation + result.total_selection + result.total_interaction - result.total_excess_return) < 1e-9


def test_brinson_pure_allocation_effect():
    """Pure allocation: portfolio and benchmark have same per-group returns → selection=0."""
    dt = date(2023, 1, 31)
    # Two sectors; portfolio overweights Tech
    pw = pd.DataFrame({
        "date": [dt, dt, dt, dt],
        "ticker": ["A", "B", "C", "D"],
        "weight": [0.6, 0.1, 0.2, 0.1],
        "sector": ["Tech", "Tech", "Energy", "Energy"],
    })
    bw = pd.DataFrame({
        "date": [dt, dt, dt, dt],
        "ticker": ["A", "B", "C", "D"],
        "weight": [0.3, 0.2, 0.3, 0.2],
        "sector": ["Tech", "Tech", "Energy", "Energy"],
    })
    # Same returns in both portfolio and benchmark tickers → selection=0 by construction
    rets = pd.DataFrame({
        "date": [dt, dt, dt, dt],
        "ticker": ["A", "B", "C", "D"],
        "return": [0.05, 0.05, 0.02, 0.02],
    })
    result = compute_brinson_attribution(pw, bw, rets)
    assert abs(result.total_selection) < 1e-9
    assert abs(result.total_interaction) < 1e-9
    assert abs(result.total_allocation) > 1e-9


def test_brinson_missing_cols_raises():
    with pytest.raises(ValueError, match="missing columns"):
        compute_brinson_attribution(
            pd.DataFrame({"date": [], "ticker": [], "weight": []}),  # missing sector
            pd.DataFrame({"date": [], "ticker": [], "weight": [], "sector": []}),
            pd.DataFrame({"date": [], "ticker": [], "return": []}),
        )


def test_brinson_empty_returns_empty_result():
    result = compute_brinson_attribution(
        pd.DataFrame(columns=["date", "ticker", "weight", "sector"]),
        pd.DataFrame(columns=["date", "ticker", "weight", "sector"]),
        pd.DataFrame(columns=["date", "ticker", "return"]),
    )
    assert result.total_excess_return == 0.0
    assert result.records.empty


# ------------------------------------------------------------------
# Factor decomposition
# ------------------------------------------------------------------

def _make_factor_data(n: int = 100) -> tuple[pd.Series, pd.DataFrame]:
    rng = np.random.default_rng(99)
    dates = pd.date_range("2022-01-01", periods=n, freq="B").date
    market = rng.normal(0.0005, 0.01, n)
    momentum = rng.normal(0.0002, 0.005, n)
    # Portfolio with known betas
    portfolio = 1.2 * market + 0.5 * momentum + rng.normal(0, 0.002, n)
    returns = pd.Series(portfolio, index=dates)
    factors = pd.DataFrame({"market": market, "momentum": momentum}, index=dates)
    return returns, factors


def test_factor_decomposition_returns_result():
    rets, factors = _make_factor_data(120)
    result = decompose_factor_returns(rets, factors)
    assert "market" in result.factor_betas.index
    assert "momentum" in result.factor_betas.index
    assert 0.0 <= result.r_squared <= 1.0


def test_factor_decomposition_beta_sign():
    """Market beta should be positive when portfolio is long-only."""
    rets, factors = _make_factor_data(200)
    result = decompose_factor_returns(rets, factors)
    assert result.factor_betas["market"] > 0


def test_factor_decomposition_insufficient_data_raises():
    rets = pd.Series([0.01] * 5, index=pd.date_range("2022-01-01", periods=5, freq="B").date)
    factors = pd.DataFrame({"market": [0.01] * 5}, index=pd.date_range("2022-01-01", periods=5, freq="B").date)
    with pytest.raises(ValueError, match="Insufficient overlapping dates"):
        decompose_factor_returns(rets, factors)


def test_compute_factor_contributions_shape():
    rets, factors = _make_factor_data(100)
    result = decompose_factor_returns(rets, factors)
    contribs = compute_factor_contributions(result.factor_betas, factors)
    assert contribs.shape[1] == 2  # market + momentum
    assert set(contribs.columns) == {"market", "momentum"}


def test_compute_factor_contributions_values():
    betas = pd.Series({"market": 1.2, "momentum": 0.5})
    factors = pd.DataFrame({"market": [0.01, -0.02], "momentum": [0.005, 0.003]})
    contribs = compute_factor_contributions(betas, factors)
    assert abs(contribs["market"].iloc[0] - 0.012) < 1e-9
    assert abs(contribs["momentum"].iloc[0] - 0.0025) < 1e-9

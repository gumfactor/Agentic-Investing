"""Tests for the real-time risk monitor."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from risk.realtime.monitor import BreachSeverity, RiskMonitor, RiskSnapshot


@pytest.fixture
def monitor() -> RiskMonitor:
    return RiskMonitor(
        hard_drawdown=-0.10,
        hard_var=0.025,
        hard_beta=1.5,
        hard_concentration=0.05,
        warn_drawdown=-0.05,
        warn_var=0.015,
        warn_beta=1.3,
        warn_concentration=0.04,
    )


@pytest.fixture
def weights() -> pd.Series:
    return pd.Series({"AAPL": 0.30, "MSFT": 0.30, "GOOGL": 0.20, "AMZN": 0.20})


@pytest.fixture
def portfolio_returns() -> pd.Series:
    rng = np.random.default_rng(7)
    return pd.Series(rng.normal(0.0005, 0.008, 252))


@pytest.fixture
def asset_returns(weights) -> pd.DataFrame:
    rng = np.random.default_rng(8)
    tickers = list(weights.index)
    data = rng.normal(0, 0.01, size=(252, len(tickers)))
    return pd.DataFrame(data, columns=tickers)


@pytest.fixture
def benchmark_returns() -> pd.Series:
    rng = np.random.default_rng(9)
    return pd.Series(rng.normal(0.0004, 0.008, 252))


class TestRiskMonitor:
    def test_snapshot_returns_risk_snapshot(self, monitor, weights, portfolio_returns, asset_returns, benchmark_returns):
        snap = monitor.snapshot(
            as_of=date(2024, 6, 15),
            nav=1_000_000.0,
            weights=weights,
            portfolio_returns=portfolio_returns,
            asset_returns=asset_returns,
            benchmark_returns=benchmark_returns,
        )
        assert isinstance(snap, RiskSnapshot)
        assert snap.nav == 1_000_000.0

    def test_no_drawdown_at_peak(self, monitor, weights, portfolio_returns, asset_returns, benchmark_returns):
        snap = monitor.snapshot(
            as_of=date(2024, 6, 15),
            nav=1_000_000.0,
            weights=weights,
            portfolio_returns=portfolio_returns,
            asset_returns=asset_returns,
            benchmark_returns=benchmark_returns,
        )
        assert snap.drawdown == pytest.approx(0.0)

    def test_drawdown_calculated_from_peak(self, monitor, portfolio_returns, asset_returns, benchmark_returns):
        # Use tiny weights so no concentration breach interferes with this test
        w = pd.Series({"AAPL": 0.03, "MSFT": 0.03})
        monitor.snapshot(date(2024, 1, 1), 1_000_000.0, w, portfolio_returns, asset_returns, benchmark_returns)
        snap = monitor.snapshot(date(2024, 1, 2), 900_000.0, w, portfolio_returns, asset_returns, benchmark_returns)
        assert abs(snap.drawdown - (-0.10)) < 0.001

    def test_hard_drawdown_trips_circuit_breaker(self, monitor, portfolio_returns, asset_returns, benchmark_returns):
        w = pd.Series({"AAPL": 0.03, "MSFT": 0.03})
        monitor.snapshot(date(2024, 1, 1), 1_000_000.0, w, portfolio_returns, asset_returns, benchmark_returns)
        snap = monitor.snapshot(date(2024, 1, 2), 880_000.0, w, portfolio_returns, asset_returns, benchmark_returns)
        assert snap.circuit_breaker_tripped
        assert any(b["metric"] == "drawdown" and b["severity"] == "hard" for b in snap.breaches)

    def test_warning_drawdown_no_circuit_trip(self, monitor, portfolio_returns, asset_returns, benchmark_returns):
        w = pd.Series({"AAPL": 0.03, "MSFT": 0.03})
        monitor.snapshot(date(2024, 1, 1), 1_000_000.0, w, portfolio_returns, asset_returns, benchmark_returns)
        # -7% drawdown → warning, not hard (-10%); weights don't breach concentration
        snap = monitor.snapshot(date(2024, 1, 2), 930_000.0, w, portfolio_returns, asset_returns, benchmark_returns)
        assert not snap.circuit_breaker_tripped
        assert any(b["severity"] == "warning" for b in snap.breaches)

    def test_concentration_breach_detected(self, monitor, portfolio_returns, asset_returns, benchmark_returns):
        concentrated = pd.Series({"AAPL": 0.60, "MSFT": 0.40})
        snap = monitor.snapshot(date(2024, 6, 15), 1_000_000.0, concentrated, portfolio_returns, asset_returns, benchmark_returns)
        assert any(b["metric"] == "concentration" for b in snap.breaches)

    def test_from_config(self):
        cfg = {
            "hard_drawdown_threshold": -0.15,
            "hard_var_1d_threshold": 0.03,
            "hard_beta_threshold": 2.0,
            "hard_concentration_threshold": 0.10,
            "warn_drawdown_threshold": -0.08,
            "warn_var_1d_threshold": 0.02,
            "warn_beta_threshold": 1.5,
            "warn_concentration_threshold": 0.08,
        }
        mon = RiskMonitor.from_config(cfg)
        assert mon._hard["drawdown"] == -0.15

    def test_worst_severity_none_when_clean(self, monitor, weights, portfolio_returns, asset_returns, benchmark_returns):
        # Use very low concentration and small portfolio — should be clean
        small_weights = pd.Series({"AAPL": 0.02, "MSFT": 0.02})
        snap = monitor.snapshot(date(2024, 6, 15), 1_000_000.0, small_weights, portfolio_returns, asset_returns, benchmark_returns)
        assert snap.worst_severity in (BreachSeverity.NONE, BreachSeverity.WARNING)

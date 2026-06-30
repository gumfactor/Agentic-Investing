"""Tests for Sprint 2 risk monitor logic — pure Python, no Streamlit dependency.

Tests risk computation, alert management, and circuit breaker reset flow.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from risk.alerts.alert_manager import AlertManager
from risk.circuit_breaker import CircuitBreaker
from risk.realtime.monitor import RiskMonitor, RiskSnapshot


class TestRiskSnapshotComputation:
    def _make_data(self, n_days: int = 60):
        """Create minimal data for risk computation."""
        import numpy as np
        np.random.seed(42)
        dates = pd.date_range("2026-01-01", periods=n_days, freq="B")
        tickers = ["AAPL", "MSFT", "GOOG"]
        asset_returns = pd.DataFrame(
            np.random.normal(0.001, 0.02, (n_days, len(tickers))),
            index=dates,
            columns=tickers,
        )
        benchmark = pd.Series(
            np.random.normal(0.0005, 0.01, n_days),
            index=dates,
        )
        weights = pd.Series({"AAPL": 0.4, "MSFT": 0.35, "GOOG": 0.25})
        port_returns = asset_returns.dot(weights)
        return weights, port_returns, asset_returns, benchmark

    def test_snapshot_returns_all_metrics(self):
        weights, port_rets, asset_rets, bench_rets = self._make_data()
        monitor = RiskMonitor()

        snap = monitor.snapshot(
            as_of=date(2026, 6, 29),
            nav=100_000.0,
            weights=weights,
            portfolio_returns=port_rets,
            asset_returns=asset_rets,
            benchmark_returns=bench_rets,
        )

        assert isinstance(snap, RiskSnapshot)
        assert snap.nav == 100_000.0
        assert snap.var_1d_99 >= 0
        assert snap.cvar_1d_99 >= 0
        assert -1 < snap.drawdown <= 0
        assert isinstance(snap.portfolio_beta, float)
        assert 0 <= snap.max_concentration <= 1

    def test_drawdown_tracked(self):
        weights, port_rets, asset_rets, bench_rets = self._make_data()
        monitor = RiskMonitor(peak_nav=110_000.0)

        snap = monitor.snapshot(
            as_of=date(2026, 6, 29),
            nav=100_000.0,
            weights=weights,
            portfolio_returns=port_rets,
            asset_returns=asset_rets,
            benchmark_returns=bench_rets,
        )

        expected_dd = (100_000.0 / 110_000.0) - 1.0
        assert abs(snap.drawdown - expected_dd) < 0.001

    def test_breach_detection(self):
        weights, port_rets, asset_rets, bench_rets = self._make_data()
        # Set very tight thresholds to force breaches
        monitor = RiskMonitor(
            hard_drawdown=-0.001,
            warn_drawdown=-0.0005,
            peak_nav=101_000.0,
        )

        snap = monitor.snapshot(
            as_of=date(2026, 6, 29),
            nav=100_000.0,
            weights=weights,
            portfolio_returns=port_rets,
            asset_returns=asset_rets,
            benchmark_returns=bench_rets,
        )

        assert len(snap.breaches) > 0
        assert snap.circuit_breaker_tripped


class TestAlertManagerIntegration:
    def test_fire_and_unacknowledged(self):
        am = AlertManager()
        alert = am.fire("warning", "var_1d", 0.02, 0.015)
        assert alert is not None
        assert len(am.unacknowledged()) == 1

    def test_acknowledge(self):
        am = AlertManager()
        alert = am.fire("hard", "drawdown", -0.12, -0.10)
        assert alert is not None
        assert am.acknowledge(alert.alert_id)
        assert len(am.unacknowledged()) == 0
        assert len(am.all_alerts()) == 1

    def test_fire_from_snapshot(self):
        am = AlertManager()
        snap = MagicMock()
        snap.breaches = [
            {"severity": "hard", "metric": "drawdown", "value": -0.12, "threshold": -0.10},
            {"severity": "warning", "metric": "var_1d", "value": 0.018, "threshold": 0.015},
        ]
        fired = am.fire_from_snapshot(snap)
        assert len(fired) == 2
        assert len(am.unacknowledged()) == 2


class TestCircuitBreakerResetFlow:
    def test_reset_requires_operator(self):
        cb = CircuitBreaker()
        snap = MagicMock()
        snap.circuit_breaker_tripped = True
        snap.as_of = date(2026, 6, 29)
        snap.breaches = [
            {"severity": "hard", "metric": "drawdown", "value": -0.12, "threshold": -0.10}
        ]
        cb.evaluate(snap)
        assert cb.is_open

        with pytest.raises(ValueError):
            cb.reset(operator="", reason_code="test reason code")

    def test_reset_requires_reason(self):
        cb = CircuitBreaker()
        snap = MagicMock()
        snap.circuit_breaker_tripped = True
        snap.as_of = date(2026, 6, 29)
        snap.breaches = [
            {"severity": "hard", "metric": "var", "value": 0.03, "threshold": 0.025}
        ]
        cb.evaluate(snap)

        with pytest.raises(ValueError):
            cb.reset(operator="op@test.com", reason_code="")

    def test_successful_reset(self):
        cb = CircuitBreaker()
        snap = MagicMock()
        snap.circuit_breaker_tripped = True
        snap.as_of = date(2026, 6, 29)
        snap.breaches = [
            {"severity": "hard", "metric": "beta", "value": 1.6, "threshold": 1.5}
        ]
        cb.evaluate(snap)
        assert cb.is_open

        cb.reset(operator="op@test.com", reason_code="drill complete, all clear")
        assert cb.is_closed

    def test_trip_history_persists_after_reset(self):
        cb = CircuitBreaker()
        snap = MagicMock()
        snap.circuit_breaker_tripped = True
        snap.as_of = date(2026, 6, 29)
        snap.breaches = [
            {"severity": "hard", "metric": "concentration", "value": 0.08, "threshold": 0.05}
        ]
        cb.evaluate(snap)
        cb.reset(operator="op@test.com", reason_code="reviewed and cleared")

        history = cb.trip_history()
        assert len(history) == 1

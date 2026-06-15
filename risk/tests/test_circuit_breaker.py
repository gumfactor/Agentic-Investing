"""Tests for the circuit breaker (safety rule C4)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from risk.circuit_breaker import CircuitBreaker, CircuitBreakerState
from risk.realtime.monitor import RiskSnapshot


def _make_snapshot(circuit_tripped: bool, breaches: list | None = None) -> RiskSnapshot:
    """Create a minimal RiskSnapshot for testing."""
    return RiskSnapshot(
        as_of=date(2024, 6, 15),
        nav=1_000_000.0,
        drawdown=-0.05,
        var_1d_99=0.02,
        cvar_1d_99=0.025,
        portfolio_beta=1.1,
        max_concentration=0.04,
        max_sector_concentration=0.20,
        breaches=breaches or [],
        circuit_breaker_tripped=circuit_tripped,
    )


class TestCircuitBreaker:
    def test_initially_closed(self):
        cb = CircuitBreaker()
        assert cb.is_closed
        assert cb.state == CircuitBreakerState.CLOSED

    def test_trips_on_hard_breach(self):
        cb = CircuitBreaker()
        snap = _make_snapshot(
            circuit_tripped=True,
            breaches=[{"metric": "drawdown", "severity": "hard", "value": -0.11, "threshold": -0.10}],
        )
        tripped = cb.evaluate(snap)
        assert tripped
        assert cb.is_open

    def test_does_not_trip_on_no_breach(self):
        cb = CircuitBreaker()
        snap = _make_snapshot(circuit_tripped=False)
        cb.evaluate(snap)
        assert cb.is_closed

    def test_does_not_auto_reset(self):
        cb = CircuitBreaker()
        snap = _make_snapshot(
            circuit_tripped=True,
            breaches=[{"metric": "var_1d", "severity": "hard", "value": 0.03, "threshold": 0.025}],
        )
        cb.evaluate(snap)
        # After breach clears, evaluating a clean snapshot must NOT close the breaker
        clean = _make_snapshot(circuit_tripped=False)
        cb.evaluate(clean)
        assert cb.is_open, "Circuit breaker must stay OPEN until a human resets it (C4)"

    def test_human_reset_requires_operator(self):
        cb = CircuitBreaker()
        snap = _make_snapshot(
            circuit_tripped=True,
            breaches=[{"metric": "drawdown", "severity": "hard", "value": -0.12, "threshold": -0.10}],
        )
        cb.evaluate(snap)
        with pytest.raises(ValueError, match="operator"):
            cb.reset(operator="", reason_code="RISK_CLEARED")

    def test_human_reset_requires_reason_code(self):
        cb = CircuitBreaker()
        snap = _make_snapshot(circuit_tripped=True, breaches=[{"metric": "drawdown", "severity": "hard", "value": -0.12, "threshold": -0.10}])
        cb.evaluate(snap)
        with pytest.raises(ValueError, match="reason_code"):
            cb.reset(operator="alice@firm.com", reason_code="")

    def test_valid_reset_closes_breaker(self):
        cb = CircuitBreaker()
        snap = _make_snapshot(circuit_tripped=True, breaches=[{"metric": "drawdown", "severity": "hard", "value": -0.12, "threshold": -0.10}])
        cb.evaluate(snap)
        cb.reset(operator="alice@firm.com", reason_code="RISK_CLEARED_BY_OPS")
        assert cb.is_closed

    def test_reset_records_history(self):
        cb = CircuitBreaker()
        snap = _make_snapshot(circuit_tripped=True, breaches=[{"metric": "drawdown", "severity": "hard", "value": -0.12, "threshold": -0.10}])
        cb.evaluate(snap)
        cb.reset(operator="bob@firm.com", reason_code="EOD_MANUAL_RESET")
        assert len(cb.reset_history()) == 1
        assert cb.reset_history()[0].operator == "bob@firm.com"

    def test_reset_already_closed_raises(self):
        cb = CircuitBreaker()
        with pytest.raises(RuntimeError, match="already CLOSED"):
            cb.reset(operator="alice@firm.com", reason_code="TEST")

    def test_trip_records_history(self):
        cb = CircuitBreaker()
        snap = _make_snapshot(circuit_tripped=True, breaches=[{"metric": "concentration", "severity": "hard", "value": 0.06, "threshold": 0.05}])
        cb.evaluate(snap)
        assert len(cb.trip_history()) == 1
        assert cb.trip_history()[0].metric == "concentration"

    def test_status_dict(self):
        cb = CircuitBreaker()
        status = cb.status_dict()
        assert status["state"] == "CLOSED"
        assert status["is_open"] is False
        assert status["n_trips"] == 0

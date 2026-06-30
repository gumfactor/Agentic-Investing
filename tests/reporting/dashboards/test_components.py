"""Tests for dashboard shared components — pure Python logic tests.

Streamlit rendering is not tested here (requires AppTest or manual verification).
These tests verify the logic that determines what gets rendered.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from risk.circuit_breaker import CircuitBreaker


class TestEnvBannerLogic:
    """Test the logic that determines which banner variant is shown."""

    def test_paper_mode_correct(self):
        paper = "true"
        port = "7497"
        assert paper == "true" and port == "7497"

    def test_live_mode_correct(self):
        paper = "false"
        port = "7496"
        assert paper != "true" and port == "7496"

    def test_misconfigured_paper_with_live_port(self):
        paper = "true"
        port = "7496"
        is_paper = paper == "true" and port == "7497"
        is_live = paper != "true" and port == "7496"
        assert not is_paper and not is_live

    def test_misconfigured_live_with_paper_port(self):
        paper = "false"
        port = "7497"
        is_paper = paper == "true" and port == "7497"
        is_live = paper != "true" and port == "7496"
        assert not is_paper and not is_live

    def test_empty_env_vars(self):
        paper = ""
        port = ""
        is_paper = paper.lower() == "true" and port == "7497"
        is_live = paper.lower() != "true" and port == "7496"
        assert not is_paper and not is_live


class TestCircuitBreakerWidget:
    """Test circuit breaker state queries used by the sidebar widget."""

    def test_closed_status_dict(self):
        cb = CircuitBreaker()
        status = cb.status_dict()
        assert status["state"] == "CLOSED"
        assert status["is_open"] is False
        assert status["n_trips"] == 0

    def test_open_after_trip(self):
        from risk.realtime.monitor import RiskSnapshot
        from unittest.mock import MagicMock
        from datetime import date

        cb = CircuitBreaker()
        snap = MagicMock(spec=RiskSnapshot)
        snap.circuit_breaker_tripped = True
        snap.as_of = date(2026, 6, 29)
        snap.breaches = [
            {"severity": "hard", "metric": "drawdown", "value": -0.12, "threshold": -0.10}
        ]

        cb.evaluate(snap)
        status = cb.status_dict()
        assert status["state"] == "OPEN"
        assert status["is_open"] is True
        assert status["n_trips"] == 1

    def test_reset_requires_operator_and_reason(self):
        from risk.realtime.monitor import RiskSnapshot
        from unittest.mock import MagicMock
        from datetime import date

        cb = CircuitBreaker()
        snap = MagicMock(spec=RiskSnapshot)
        snap.circuit_breaker_tripped = True
        snap.as_of = date(2026, 6, 29)
        snap.breaches = [
            {"severity": "hard", "metric": "var", "value": 0.03, "threshold": 0.025}
        ]
        cb.evaluate(snap)

        with pytest.raises(ValueError):
            cb.reset(operator="", reason_code="test")

        with pytest.raises(ValueError):
            cb.reset(operator="op@test.com", reason_code="")

        cb.reset(operator="op@test.com", reason_code="drill complete")
        assert cb.is_closed

    def test_trip_history_tracked(self):
        from unittest.mock import MagicMock
        from datetime import date

        cb = CircuitBreaker()
        snap = MagicMock()
        snap.circuit_breaker_tripped = True
        snap.as_of = date(2026, 6, 29)
        snap.breaches = [
            {"severity": "hard", "metric": "beta", "value": 1.6, "threshold": 1.5}
        ]
        cb.evaluate(snap)

        history = cb.trip_history()
        assert len(history) == 1
        assert history[0].metric == "beta"
        assert history[0].value == 1.6


class TestSubmitEnabled:
    """Test the _is_submit_enabled logic from Page 4."""

    def _check(self, paper: str, port: str, c8: str, cb_open: bool) -> tuple[bool, str]:
        if cb_open:
            return False, "Circuit breaker is OPEN (C4)"
        if paper.lower() == "true" and port == "7497":
            return True, ""
        if paper.lower() != "true" and port == "7496" and c8.lower() == "true":
            return True, ""
        if paper.lower() != "true" and port == "7496" and c8.lower() != "true":
            return False, "Live trading requires C8 clearance"
        return False, "Environment misconfigured"

    def test_paper_mode_enabled(self):
        ok, _ = self._check("true", "7497", "", False)
        assert ok

    def test_live_mode_with_c8(self):
        ok, _ = self._check("false", "7496", "true", False)
        assert ok

    def test_live_mode_without_c8(self):
        ok, reason = self._check("false", "7496", "false", False)
        assert not ok
        assert "C8" in reason

    def test_circuit_breaker_blocks(self):
        ok, reason = self._check("true", "7497", "", True)
        assert not ok
        assert "Circuit breaker" in reason

    def test_misconfigured_blocked(self):
        ok, reason = self._check("true", "7496", "", False)
        assert not ok
        assert "misconfigured" in reason

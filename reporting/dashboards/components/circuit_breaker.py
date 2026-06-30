"""Circuit breaker and alert manager singletons — rendered on every page.

Uses @st.cache_resource so all tabs share single instances
with consistent state (spec Section 5.2).
"""
from __future__ import annotations

import streamlit as st

from risk.alerts.alert_manager import AlertManager
from risk.circuit_breaker import CircuitBreaker


@st.cache_resource
def get_circuit_breaker() -> CircuitBreaker:
    return CircuitBreaker()


@st.cache_resource
def get_alert_manager() -> AlertManager:
    return AlertManager()


def render_circuit_breaker_sidebar() -> None:
    cb = get_circuit_breaker()
    status = cb.status_dict()

    if status["is_open"]:
        trips = cb.trip_history()
        last = trips[-1] if trips else None
        detail = ""
        if last:
            detail = (
                f" — {last.metric} breached {last.value:.4f} "
                f"at {last.tripped_at:%Y-%m-%d %H:%M} UTC"
            )
        st.sidebar.error(f"CIRCUIT BREAKER OPEN{detail}")
    else:
        st.sidebar.success("Circuit Breaker: CLOSED ✓")


def render_circuit_breaker_warning() -> None:
    """Non-dismissable warning when CB is open — shown below the env banner."""
    cb = get_circuit_breaker()
    if cb.is_open:
        st.warning(
            "Trading is halted. No orders can be submitted until the "
            "circuit breaker is reset by the operator.",
            icon="⚠️",
        )

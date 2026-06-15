"""Circuit breaker — halts all order submission on a hard risk breach.

Safety rule C4: The circuit breaker can ONLY be reset by a human operator
who supplies a reason code.  It must NEVER be reset automatically.

State machine:
    CLOSED  → all clear, orders flow normally
    OPEN    → hard breach detected; all order submission blocked
    TESTING → half-open state while operator manually validates conditions
              before fully re-closing (future enhancement, not implemented here)

The circuit breaker is intentionally decoupled from the monitoring layer:
RiskMonitor returns a RiskSnapshot; the circuit breaker decides whether
to trip based on that snapshot.  This separation makes both testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


class CircuitBreakerState(str, Enum):
    CLOSED = "CLOSED"   # normal — orders flow
    OPEN = "OPEN"       # halted — orders blocked


@dataclass
class TripEvent:
    """Record of when and why the circuit breaker tripped."""

    tripped_at: datetime
    metric: str
    value: float
    threshold: float
    snapshot_date: str


@dataclass
class ResetEvent:
    """Record of a human-authorized circuit-breaker reset."""

    reset_at: datetime
    operator: str
    reason_code: str


class CircuitBreaker:
    """Manages the CLOSED / OPEN trading halt state.

    Usage::

        cb = CircuitBreaker()
        snap = monitor.snapshot(...)
        cb.evaluate(snap)          # trips if hard breach in snap
        if cb.is_open:
            # Blocks order submission (compliance also checks this)
            raise RuntimeError("Circuit breaker is OPEN")

        # Human reset only:
        cb.reset(operator="alice@firm.com", reason_code="RISK_CLEARED_BY_OPS")
    """

    def __init__(self) -> None:
        self._state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self._trip_history: list[TripEvent] = []
        self._reset_history: list[ResetEvent] = []

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitBreakerState.OPEN

    @property
    def is_closed(self) -> bool:
        return self._state == CircuitBreakerState.CLOSED

    def evaluate(self, snapshot: "RiskSnapshot") -> bool:  # type: ignore[name-defined]
        """Check snapshot and trip if a hard breach is present.

        Returns True if the circuit breaker was (or remains) tripped.
        """
        from risk.realtime.monitor import RiskSnapshot

        if snapshot.circuit_breaker_tripped and self._state == CircuitBreakerState.CLOSED:
            worst_breach = next(
                (b for b in snapshot.breaches if b["severity"] == "hard"), None
            )
            metric = worst_breach["metric"] if worst_breach else "unknown"
            value = worst_breach["value"] if worst_breach else 0.0
            threshold = worst_breach["threshold"] if worst_breach else 0.0

            self._trip(
                metric=metric,
                value=value,
                threshold=threshold,
                snapshot_date=snapshot.as_of.isoformat(),
            )
            return True

        return self.is_open

    def _trip(
        self,
        metric: str,
        value: float,
        threshold: float,
        snapshot_date: str,
    ) -> None:
        self._state = CircuitBreakerState.OPEN
        event = TripEvent(
            tripped_at=datetime.now(timezone.utc),
            metric=metric,
            value=value,
            threshold=threshold,
            snapshot_date=snapshot_date,
        )
        self._trip_history.append(event)
        logger.error(
            "circuit_breaker_tripped",
            metric=metric,
            value=round(value, 6),
            threshold=round(threshold, 6),
            snapshot_date=snapshot_date,
            message=(
                "ALL ORDER SUBMISSION HALTED. "
                "A human operator must call circuit_breaker.reset() with a reason code (C4)."
            ),
        )

    def reset(self, operator: str, reason_code: str) -> None:
        """Re-close the circuit breaker.

        Parameters
        ----------
        operator:
            Identity of the human resetting the breaker (email or username).
        reason_code:
            Machine-readable code documenting why the reset is authorized.
            Examples: 'RISK_CLEARED_BY_OPS', 'MARKET_CLOSE_EOD', 'FALSE_POSITIVE'

        Raises
        ------
        ValueError if operator or reason_code are empty strings.
        RuntimeError if the circuit breaker is already CLOSED.
        """
        if not operator or not operator.strip():
            raise ValueError("operator must be a non-empty string (C4)")
        if not reason_code or not reason_code.strip():
            raise ValueError("reason_code must be a non-empty string (C4)")
        if self._state == CircuitBreakerState.CLOSED:
            raise RuntimeError("Circuit breaker is already CLOSED.")

        event = ResetEvent(
            reset_at=datetime.now(timezone.utc),
            operator=operator.strip(),
            reason_code=reason_code.strip(),
        )
        self._reset_history.append(event)
        self._state = CircuitBreakerState.CLOSED

        logger.warning(
            "circuit_breaker_reset",
            operator=event.operator,
            reason_code=event.reason_code,
            reset_at=event.reset_at.isoformat(),
        )

    def trip_history(self) -> list[TripEvent]:
        return list(self._trip_history)

    def reset_history(self) -> list[ResetEvent]:
        return list(self._reset_history)

    def status_dict(self) -> dict:
        """Return a human-readable status summary."""
        return {
            "state": self._state.value,
            "is_open": self.is_open,
            "n_trips": len(self._trip_history),
            "n_resets": len(self._reset_history),
            "last_trip": self._trip_history[-1].snapshot_date if self._trip_history else None,
            "last_reset_by": self._reset_history[-1].operator if self._reset_history else None,
        }

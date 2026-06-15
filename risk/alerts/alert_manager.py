"""Alert manager — dispatches risk breach notifications.

Alerts are appended to an in-memory log and optionally dispatched via
configured channels (structured log, future: email / Slack).

Design: alerts are fire-and-forget; the circuit breaker (circuit_breaker.py)
is the authoritative source of trading halts.  Alerts are informational.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

import structlog

logger = structlog.get_logger(__name__)


class AlertChannel(str, Enum):
    LOG = "log"         # structlog (always active)
    EMAIL = "email"     # future
    SLACK = "slack"     # future


@dataclass
class Alert:
    """A single risk alert."""

    alert_id: str
    severity: str           # 'warning' or 'hard'
    metric: str
    value: float
    threshold: float
    message: str
    fired_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    acknowledged: bool = False


AlertHandler = Callable[[Alert], None]


class AlertManager:
    """Dispatches and stores risk alerts.

    Parameters
    ----------
    handlers:
        Optional list of callables that receive each Alert.  The default
        handler is a structlog emit.
    """

    def __init__(self, handlers: list[AlertHandler] | None = None) -> None:
        self._handlers: list[AlertHandler] = handlers or [self._default_handler]
        self._alerts: list[Alert] = []
        self._counter: int = 0

    def fire(
        self,
        severity: str,
        metric: str,
        value: float,
        threshold: float,
        message: str = "",
    ) -> Alert:
        """Create and dispatch an alert.

        Returns the Alert object (stored internally and returned for testing).
        """
        self._counter += 1
        alert = Alert(
            alert_id=f"ALERT-{self._counter:06d}",
            severity=severity,
            metric=metric,
            value=value,
            threshold=threshold,
            message=message or f"{metric}={value:.4f} crossed threshold {threshold:.4f}",
        )
        self._alerts.append(alert)
        for handler in self._handlers:
            try:
                handler(alert)
            except Exception as exc:
                logger.error("alert_handler_failed", handler=handler.__name__, error=str(exc))
        return alert

    def fire_from_snapshot(self, snapshot: "RiskSnapshot") -> list[Alert]:
        """Fire alerts for all breaches in a RiskSnapshot."""
        from risk.realtime.monitor import RiskSnapshot  # local import to avoid circularity

        fired: list[Alert] = []
        for breach in snapshot.breaches:
            alert = self.fire(
                severity=breach["severity"],
                metric=breach["metric"],
                value=breach["value"],
                threshold=breach["threshold"],
            )
            fired.append(alert)
        return fired

    def acknowledge(self, alert_id: str) -> bool:
        """Mark an alert as acknowledged.  Returns True if found."""
        for a in self._alerts:
            if a.alert_id == alert_id:
                a.acknowledged = True
                return True
        return False

    def unacknowledged(self) -> list[Alert]:
        return [a for a in self._alerts if not a.acknowledged]

    def all_alerts(self) -> list[Alert]:
        return list(self._alerts)

    @staticmethod
    def _default_handler(alert: Alert) -> None:
        log_fn = logger.warning if alert.severity == "warning" else logger.error
        log_fn(
            "risk_alert",
            alert_id=alert.alert_id,
            severity=alert.severity,
            metric=alert.metric,
            value=round(alert.value, 6),
            threshold=round(alert.threshold, 6),
            message=alert.message,
        )

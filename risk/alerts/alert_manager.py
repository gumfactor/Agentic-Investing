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
    dedup_seconds:
        Minimum seconds between repeated alerts for the same metric at the
        same severity.  Prevents alert flooding when a breach persists across
        monitoring ticks.  Default: 3600 (1 hour).
    max_alerts:
        Maximum number of alerts to retain in memory.  Oldest are evicted
        once this limit is reached.  Default: 10_000.
    """

    def __init__(
        self,
        handlers: list[AlertHandler] | None = None,
        dedup_seconds: int = 3600,
        max_alerts: int = 10_000,
    ) -> None:
        self._handlers: list[AlertHandler] = handlers or [self._default_handler]
        self._alerts: list[Alert] = []
        self._counter: int = 0
        self._dedup_seconds = dedup_seconds
        self._max_alerts = max_alerts
        # (metric, severity) → last fired datetime
        self._last_fired: dict[tuple[str, str], datetime] = {}

    def fire(
        self,
        severity: str,
        metric: str,
        value: float,
        threshold: float,
        message: str = "",
        force: bool = False,
    ) -> Alert | None:
        """Create and dispatch an alert, subject to deduplication.

        Returns the Alert object (stored internally), or None if suppressed by
        the deduplication window.

        Parameters
        ----------
        force:
            If True, bypass deduplication (use for HARD severity escalations).
        """
        key = (metric, severity)
        now = datetime.now(timezone.utc)

        if not force and severity != "hard":
            last = self._last_fired.get(key)
            if last is not None:
                elapsed = (now - last).total_seconds()
                if elapsed < self._dedup_seconds:
                    return None  # suppress duplicate

        self._last_fired[key] = now
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

        # Evict oldest alerts beyond the memory cap
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts :]

        for handler in self._handlers:
            try:
                handler(alert)
            except Exception as exc:
                logger.error("alert_handler_failed", handler=handler.__name__, error=str(exc))
        return alert

    def fire_from_snapshot(self, snapshot: object) -> list[Alert]:
        """Fire alerts for all breaches in a RiskSnapshot.

        Hard-severity alerts always bypass the deduplication window (force=True)
        so hard breaches are never silently suppressed.
        """
        fired: list[Alert] = []
        for breach in getattr(snapshot, "breaches", []):
            is_hard = breach["severity"] == "hard"
            alert = self.fire(
                severity=breach["severity"],
                metric=breach["metric"],
                value=breach["value"],
                threshold=breach["threshold"],
                force=is_hard,
            )
            if alert is not None:
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

"""Tests for AlertManager — dedup, eviction, hard-alert bypass, fire_from_snapshot."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from risk.alerts.alert_manager import Alert, AlertManager


class TestAlertManagerBasic:
    def test_fire_returns_alert(self):
        am = AlertManager()
        alert = am.fire("warning", "var_1d", 0.02, 0.015)
        assert alert is not None
        assert alert.metric == "var_1d"
        assert alert.severity == "warning"
        assert alert.alert_id == "ALERT-000001"

    def test_all_alerts_accumulates(self):
        am = AlertManager()
        am.fire("warning", "beta", 1.4, 1.3)
        am.fire("hard", "drawdown", -0.12, -0.10)
        assert len(am.all_alerts()) == 2

    def test_acknowledge_marks_alert(self):
        am = AlertManager()
        alert = am.fire("warning", "var_1d", 0.02, 0.015)
        result = am.acknowledge(alert.alert_id)
        assert result is True
        assert am.all_alerts()[0].acknowledged is True

    def test_acknowledge_unknown_id_returns_false(self):
        am = AlertManager()
        assert am.acknowledge("ALERT-999999") is False

    def test_unacknowledged_filters_correctly(self):
        am = AlertManager()
        a1 = am.fire("warning", "beta", 1.4, 1.3)
        am.fire("warning", "var_1d", 0.02, 0.015, force=True)
        am.acknowledge(a1.alert_id)
        unacked = am.unacknowledged()
        assert len(unacked) == 1
        assert unacked[0].metric == "var_1d"


class TestAlertManagerDedup:
    def test_dedup_suppresses_second_warning_within_window(self):
        am = AlertManager(dedup_seconds=3600)
        a1 = am.fire("warning", "beta", 1.4, 1.3)
        a2 = am.fire("warning", "beta", 1.45, 1.3)  # same metric+severity within window
        assert a1 is not None
        assert a2 is None  # suppressed
        assert len(am.all_alerts()) == 1

    def test_dedup_allows_after_window_expires(self):
        am = AlertManager(dedup_seconds=0)  # zero-second window — never suppress
        a1 = am.fire("warning", "beta", 1.4, 1.3)
        a2 = am.fire("warning", "beta", 1.45, 1.3)
        assert a1 is not None
        assert a2 is not None

    def test_hard_severity_bypasses_dedup(self):
        am = AlertManager(dedup_seconds=3600)
        a1 = am.fire("hard", "drawdown", -0.12, -0.10)
        a2 = am.fire("hard", "drawdown", -0.13, -0.10)  # same key, within window
        assert a1 is not None
        assert a2 is not None  # hard always bypasses dedup

    def test_force_true_bypasses_dedup(self):
        am = AlertManager(dedup_seconds=3600)
        a1 = am.fire("warning", "beta", 1.4, 1.3)
        a2 = am.fire("warning", "beta", 1.45, 1.3, force=True)
        assert a1 is not None
        assert a2 is not None  # force=True bypasses dedup

    def test_different_severity_same_metric_not_deduped(self):
        am = AlertManager(dedup_seconds=3600)
        a1 = am.fire("warning", "beta", 1.35, 1.3)
        a2 = am.fire("hard", "beta", 1.55, 1.5)  # different severity key
        assert a1 is not None
        assert a2 is not None


class TestAlertManagerEviction:
    def test_max_alerts_evicts_oldest(self):
        am = AlertManager(max_alerts=3)
        for i in range(4):
            am.fire("warning", f"metric_{i}", float(i), 0.0, force=True)
        alerts = am.all_alerts()
        assert len(alerts) == 3
        # oldest (metric_0) should be evicted
        metrics = [a.metric for a in alerts]
        assert "metric_0" not in metrics
        assert "metric_3" in metrics

    def test_hard_unacknowledged_never_evicted(self):
        am = AlertManager(max_alerts=3)
        # Fire one hard unacknowledged alert first
        hard = am.fire("hard", "drawdown", -0.15, -0.10)
        # Fill past cap with warnings
        for i in range(3):
            am.fire("warning", f"metric_{i}", float(i), 0.0, force=True)
        alerts = am.all_alerts()
        alert_ids = [a.alert_id for a in alerts]
        assert hard.alert_id in alert_ids  # hard unacked preserved

    def test_acknowledged_hard_alert_can_be_evicted(self):
        am = AlertManager(max_alerts=3)
        hard = am.fire("hard", "drawdown", -0.15, -0.10)
        am.acknowledge(hard.alert_id)
        for i in range(3):
            am.fire("warning", f"metric_{i}", float(i), 0.0, force=True)
        alerts = am.all_alerts()
        alert_ids = [a.alert_id for a in alerts]
        assert hard.alert_id not in alert_ids  # acknowledged hard can be evicted


class TestFireFromSnapshot:
    def _make_snapshot(self, breaches: list[dict]) -> object:
        snap = MagicMock()
        snap.breaches = breaches
        return snap

    def test_fires_for_each_breach(self):
        am = AlertManager()
        snap = self._make_snapshot([
            {"severity": "warning", "metric": "beta", "value": 1.35, "threshold": 1.3},
            {"severity": "hard", "metric": "drawdown", "value": -0.12, "threshold": -0.10},
        ])
        fired = am.fire_from_snapshot(snap)
        assert len(fired) == 2

    def test_hard_breach_bypasses_dedup_in_fire_from_snapshot(self):
        am = AlertManager(dedup_seconds=3600)
        snap = self._make_snapshot([
            {"severity": "hard", "metric": "drawdown", "value": -0.12, "threshold": -0.10},
        ])
        a1 = am.fire_from_snapshot(snap)
        a2 = am.fire_from_snapshot(snap)  # second call within dedup window
        assert len(a1) == 1
        assert len(a2) == 1  # hard breach always fires

    def test_warning_breach_deduped_in_fire_from_snapshot(self):
        am = AlertManager(dedup_seconds=3600)
        snap = self._make_snapshot([
            {"severity": "warning", "metric": "beta", "value": 1.35, "threshold": 1.3},
        ])
        a1 = am.fire_from_snapshot(snap)
        a2 = am.fire_from_snapshot(snap)  # second call within dedup window
        assert len(a1) == 1
        assert len(a2) == 0  # warning suppressed by dedup

    def test_empty_breaches_returns_empty(self):
        am = AlertManager()
        snap = self._make_snapshot([])
        fired = am.fire_from_snapshot(snap)
        assert fired == []

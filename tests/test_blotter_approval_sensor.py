"""Tests for BlotterApprovalSensor."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from airflow.exceptions import AirflowException


def _make_ti(*, blotter_run_id: str = "run-abc", blotter_sha256: str = "a" * 64) -> MagicMock:
    ti = MagicMock()

    def xcom_pull(key: str, task_ids: str) -> str | None:
        if key == "blotter_run_id":
            return blotter_run_id
        if key == "blotter_sha256":
            return blotter_sha256
        return None

    ti.xcom_pull.side_effect = xcom_pull
    return ti


def _make_context(ti: MagicMock) -> dict:
    return {"ti": ti}


def _approval_row(*, sha256: str = "a" * 64) -> MagicMock:
    row = MagicMock()
    row.confirmed_blotter_sha256 = sha256
    row.selected_order_ids = [1, 2]
    row.approved_by = "mshane@thecanadalist.ca"
    row.approved_at_utc = datetime(2026, 6, 25, 8, 30, tzinfo=timezone.utc)
    return row


@pytest.fixture()
def sensor():
    from airflow.plugins.blotter_approval_sensor import BlotterApprovalSensor

    return BlotterApprovalSensor(
        task_id="wait_approval",
        blotter_run_id_task_id="build_blotter",
        blotter_sha256_task_id="build_blotter",
        poke_interval=300,
        timeout=28800,
        mode="reschedule",
    )


class TestBlotterApprovalSensor:
    def test_returns_false_when_no_row(self, sensor):
        ti = _make_ti()
        ctx = _make_context(ti)

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake/db"}):
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = None
            mock_engine = MagicMock()
            mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
            mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

            with patch("airflow.plugins.blotter_approval_sensor.create_engine", return_value=mock_engine):
                result = sensor.poke(ctx)

        assert result is False

    def test_returns_true_and_pushes_xcom_when_approved(self, sensor):
        ti = _make_ti(blotter_sha256="b" * 64)
        ctx = _make_context(ti)
        row = _approval_row(sha256="b" * 64)

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake/db"}):
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = row
            mock_engine = MagicMock()
            mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
            mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

            with patch("airflow.plugins.blotter_approval_sensor.create_engine", return_value=mock_engine):
                result = sensor.poke(ctx)

        assert result is True
        ti.xcom_push.assert_any_call(key="selected_order_ids", value=[1, 2])
        ti.xcom_push.assert_any_call(key="approved_by", value="mshane@thecanadalist.ca")

    def test_raises_on_sha256_mismatch(self, sensor):
        ti = _make_ti(blotter_sha256="a" * 64)
        ctx = _make_context(ti)
        row = _approval_row(sha256="b" * 64)  # different SHA-256

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake/db"}):
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = row
            mock_engine = MagicMock()
            mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
            mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

            with patch("airflow.plugins.blotter_approval_sensor.create_engine", return_value=mock_engine):
                with pytest.raises(AirflowException, match="SHA-256 mismatch"):
                    sensor.poke(ctx)

    def test_raises_when_blotter_run_id_missing(self, sensor):
        ti = _make_ti(blotter_run_id="")
        ctx = _make_context(ti)

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake/db"}):
            with pytest.raises(AirflowException, match="blotter_run_id XCom not found"):
                sensor.poke(ctx)

    def test_raises_when_database_url_missing(self, sensor):
        ti = _make_ti()
        ctx = _make_context(ti)

        env = {k: v for k, v in __import__("os").environ.items() if k != "DATABASE_URL"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(AirflowException, match="DATABASE_URL"):
                sensor.poke(ctx)

    def test_defaults_sha256_task_id_to_run_id_task_id(self):
        from airflow.plugins.blotter_approval_sensor import BlotterApprovalSensor

        s = BlotterApprovalSensor(
            task_id="test",
            blotter_run_id_task_id="build_blotter",
        )
        assert s.blotter_sha256_task_id == "build_blotter"

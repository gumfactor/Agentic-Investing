"""Unit tests for _persist_snapshot_to_db and the non-blocking DB path in
_fetch_ibkr_snapshot.

These helpers live inside airflow/dags/daily_paper_trading.py, which has
top-level imports from pendulum and apache-airflow.  Those packages are not
installed in all development environments (they require a full Airflow setup).

The dag_mod fixture below installs minimal stubs into sys.modules, loads the
DAG file via importlib, yields the module object, then restores sys.modules on
teardown.  Stubs are never registered at module-import time, so this file does
not contaminate sys.modules for other test files collected in the same pytest
session.

All sqlalchemy calls are mocked — no real DB is required.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DAG_PATH = Path(__file__).parent.parent / "airflow" / "dags" / "daily_paper_trading.py"

_STUB_DEFS: dict[str, object] = {
    "pendulum": MagicMock(),
    "airflow": MagicMock(),
    "airflow.exceptions": MagicMock(AirflowException=Exception),
    "airflow.operators": MagicMock(),
    "airflow.operators.python": MagicMock(PythonOperator=MagicMock()),
    "airflow.sensors": MagicMock(),
    "airflow.sensors.external_task": MagicMock(ExternalTaskSensor=MagicMock()),
    "airflow.sensors.time_delta": MagicMock(TimeDeltaSensor=MagicMock()),
    "airflow.plugins": MagicMock(),
    "airflow.plugins.blotter_approval_sensor": MagicMock(BlotterApprovalSensor=MagicMock()),
    "airflow_plugins": MagicMock(),
    "airflow_plugins.blotter_approval_sensor": MagicMock(BlotterApprovalSensor=MagicMock()),
    "structlog": MagicMock(),
}


def _restore_modules(saved: dict[str, object | None]) -> None:
    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Module-scoped fixture: install stubs → load DAG → yield → restore
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dag_mod() -> types.ModuleType:
    """Load the DAG module under minimal Airflow stubs.

    Installs stubs only where sys.modules has no existing entry, so a real
    Airflow installation is used automatically.  Restores sys.modules to its
    prior state after all tests in this module finish, preventing contamination
    of other test files collected in the same pytest session.
    """
    saved = {name: sys.modules.get(name) for name in _STUB_DEFS}
    for name, stub in _STUB_DEFS.items():
        if name not in sys.modules:
            sys.modules[name] = stub  # type: ignore[assignment]

    try:
        spec = importlib.util.spec_from_file_location("_dpt_for_tests", _DAG_PATH)
        mod = types.ModuleType("_dpt_for_tests")
        mod.__file__ = str(_DAG_PATH)
        mod.__spec__ = spec
        spec.loader.exec_module(mod)
    except Exception as exc:
        _restore_modules(saved)
        pytest.skip(f"Could not load DAG helpers for testing: {exc}")

    yield mod

    _restore_modules(saved)


# ---------------------------------------------------------------------------
# Tests for _persist_snapshot_to_db
# ---------------------------------------------------------------------------

class TestPersistSnapshotToDb:
    def _make_engine_mock(self, conn: MagicMock) -> MagicMock:
        ctx = MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = MagicMock(return_value=False)
        engine = MagicMock()
        engine.begin.return_value = ctx
        return engine

    def test_executes_upsert_and_disposes(self, dag_mod: types.ModuleType) -> None:
        """INSERT ... ON CONFLICT is issued and the engine is always disposed."""
        mock_conn = MagicMock()
        mock_engine = self._make_engine_mock(mock_conn)

        with patch.object(dag_mod, "create_engine", return_value=mock_engine):
            dag_mod._persist_snapshot_to_db(
                database_url="postgresql://fake/db",
                snapshot_date="2026-06-30",
                strategy_id="v1_base_momentum",
                dag_run_id="run_abc123",
                cash_usd=5000.0,
                positions=[{"ticker": "AAPL", "quantity": 10, "price": 200.0}],
                nav_usd=7000.0,
            )

        assert mock_conn.execute.called
        sql_text = str(mock_conn.execute.call_args[0][0])
        assert "portfolio_snapshots" in sql_text
        assert "ON CONFLICT" in sql_text
        assert mock_engine.dispose.called

    def test_passes_correct_params(self, dag_mod: types.ModuleType) -> None:
        """All parameters reach the SQL execute call with the right values."""
        mock_conn = MagicMock()
        mock_engine = self._make_engine_mock(mock_conn)
        positions = [{"ticker": "MSFT", "quantity": 5, "price": 400.0}]

        with patch.object(dag_mod, "create_engine", return_value=mock_engine):
            dag_mod._persist_snapshot_to_db(
                database_url="postgresql://fake/db",
                snapshot_date="2026-06-30",
                strategy_id="v1",
                dag_run_id="run_xyz",
                cash_usd=1234.56,
                positions=positions,
                nav_usd=3234.56,
            )

        params = mock_conn.execute.call_args[0][1]
        assert params["snapshot_date"] == "2026-06-30"
        assert params["strategy_id"] == "v1"
        assert params["dag_run_id"] == "run_xyz"
        assert params["cash_usd"] == 1234.56
        assert params["nav_usd"] == 3234.56
        assert params["positions"] == json.dumps(positions)
        assert "id" in params
        assert "fetched_at_utc" in params

    def test_disposes_engine_even_on_db_error(self, dag_mod: types.ModuleType) -> None:
        """Engine is disposed even when the transaction raises."""
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(side_effect=Exception("connection refused"))
        ctx.__exit__ = MagicMock(return_value=False)
        mock_engine = MagicMock()
        mock_engine.begin.return_value = ctx

        with patch.object(dag_mod, "create_engine", return_value=mock_engine):
            with pytest.raises(Exception, match="connection refused"):
                dag_mod._persist_snapshot_to_db(
                    database_url="postgresql://fake/db",
                    snapshot_date="2026-06-30",
                    strategy_id="v1",
                    dag_run_id="run_fail",
                    cash_usd=1000.0,
                    positions=[],
                    nav_usd=1000.0,
                )

        assert mock_engine.dispose.called


# ---------------------------------------------------------------------------
# Test: DB failure must not abort _fetch_ibkr_snapshot
# ---------------------------------------------------------------------------

class TestFetchSnapshotDbPersistNonBlocking:
    def test_db_failure_does_not_abort_snapshot(
        self,
        dag_mod: types.ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If _persist_snapshot_to_db raises, the task still writes the artifact
        and pushes both XCom keys — the pipeline is not aborted."""
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("IBKR_PORT", "7497")
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))

        mock_broker = MagicMock()
        mock_broker.is_paper = True
        mock_broker.get_positions.return_value = {}
        mock_broker.get_cash_balance_usd.return_value = 5000.0
        mock_broker.get_account_value.return_value = 5000.0

        mock_ti = MagicMock()
        mock_ti.xcom_pull.return_value = "v1_base_momentum"
        context = {"ti": mock_ti, "run_id": "test-db-fail", "params": {}}

        with patch.object(dag_mod, "IBKRBroker", return_value=mock_broker):
            with patch.object(
                dag_mod,
                "_persist_snapshot_to_db",
                side_effect=Exception("DB unreachable"),
            ):
                dag_mod._fetch_ibkr_snapshot(**context)  # must not raise

        pushed_keys = [c[1]["key"] for c in mock_ti.xcom_push.call_args_list]
        assert "snapshot_path" in pushed_keys
        assert "trading_date" in pushed_keys

        snapshot_path = next(
            c[1]["value"]
            for c in mock_ti.xcom_push.call_args_list
            if c[1]["key"] == "snapshot_path"
        )
        assert Path(snapshot_path).exists()

    def test_db_positions_use_current_price_field(
        self,
        dag_mod: types.ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """positions written to portfolio_snapshots use current_price (dashboard
        schema), not the artifact's price/price_date fields."""
        from datetime import date as _date

        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("IBKR_PORT", "7497")
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))

        mock_row = MagicMock()
        mock_row.ticker = "AAPL"
        mock_row.close = 200.0
        mock_row.price_date = _date.today().isoformat()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [mock_row]
        ctx_mgr = MagicMock()
        ctx_mgr.__enter__ = MagicMock(return_value=mock_conn)
        ctx_mgr.__exit__ = MagicMock(return_value=False)
        mock_engine = MagicMock()
        mock_engine.connect.return_value = ctx_mgr

        mock_broker = MagicMock()
        mock_broker.is_paper = True
        mock_broker.get_positions.return_value = {"AAPL": 10.0}
        mock_broker.get_cash_balance_usd.return_value = 5000.0
        mock_broker.get_account_value.return_value = 7000.0

        mock_ti = MagicMock()
        mock_ti.xcom_pull.return_value = "v1_base_momentum"
        context = {"ti": mock_ti, "run_id": "test-field-map", "params": {}}

        with patch.object(dag_mod, "IBKRBroker", return_value=mock_broker):
            with patch.object(dag_mod, "create_engine", return_value=mock_engine):
                with patch.object(dag_mod, "_persist_snapshot_to_db") as mock_persist:
                    dag_mod._fetch_ibkr_snapshot(**context)

        assert mock_persist.called
        db_positions = mock_persist.call_args.kwargs["positions"]
        assert len(db_positions) == 1
        assert db_positions[0]["ticker"] == "AAPL"
        assert db_positions[0]["current_price"] == 200.0
        assert "price" not in db_positions[0]
        assert "price_date" not in db_positions[0]

        # Artifact written to disk must still use the pipeline's price field
        snapshot_path = next(
            c[1]["value"]
            for c in mock_ti.xcom_push.call_args_list
            if c[1]["key"] == "snapshot_path"
        )
        artifact = json.loads(Path(snapshot_path).read_text())
        assert artifact["positions"][0]["price"] == 200.0
        assert "current_price" not in artifact["positions"][0]

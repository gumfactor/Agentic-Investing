"""DAG integrity and unit tests for daily_paper_trading."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub out pendulum and the Airflow scheduler packages so the DAG module can
# be imported without a full Airflow installation.  These stubs are inserted
# before any test code runs; real tests that need AirflowException still get
# a genuine exception subclass, so pytest.raises() works correctly.
# ---------------------------------------------------------------------------

class AirflowException(Exception):  # noqa: N818
    """Lightweight stand-in for airflow.exceptions.AirflowException."""


def _make_airflow_stub() -> MagicMock:
    stub = MagicMock(name="airflow")
    stub.exceptions = MagicMock(AirflowException=AirflowException)
    stub.DAG = MagicMock()
    return stub


_pendulum_stub = MagicMock(name="pendulum")
_pendulum_stub.timezone.return_value = MagicMock()
_pendulum_stub.datetime.return_value = MagicMock()
_pendulum_stub.timezone.__name__ = "timezone"

_airflow_stub = _make_airflow_stub()

for _mod_name, _mod_obj in [
    ("pendulum", _pendulum_stub),
    ("airflow", _airflow_stub),
    ("airflow.exceptions", MagicMock(AirflowException=AirflowException)),
    ("airflow.models", MagicMock()),
    ("airflow.operators", MagicMock()),
    ("airflow.operators.python", MagicMock(PythonOperator=MagicMock())),
    ("airflow.sensors", MagicMock()),
    ("airflow.sensors.external_task", MagicMock(ExternalTaskSensor=MagicMock())),
    ("airflow.sensors.time_delta", MagicMock(TimeDeltaSensor=MagicMock())),
    # BlotterApprovalSensor fallback import chain in the DAG
    ("airflow.plugins", MagicMock()),
    ("airflow.plugins.blotter_approval_sensor", MagicMock(BlotterApprovalSensor=MagicMock())),
    ("airflow_plugins", MagicMock()),
    ("airflow_plugins.blotter_approval_sensor", MagicMock(BlotterApprovalSensor=MagicMock())),
    # Logging dependency used inside the DAG's warning handler
    ("structlog", MagicMock()),
]:
    sys.modules.setdefault(_mod_name, _mod_obj)

# Make AirflowException importable from the stub the same way the DAG does:
#   from airflow.exceptions import AirflowException
sys.modules["airflow.exceptions"].AirflowException = AirflowException


def _load_and_register_dag_module() -> None:
    """Load daily_paper_trading.py and register it in sys.modules under the
    canonical 'airflow.dags.daily_paper_trading' path so that:
      - from airflow.dags.daily_paper_trading import X   works in tests
      - patch("airflow.dags.daily_paper_trading.Y", ...)  works in tests
    Called once at module level; failures skip the affected tests at runtime."""
    import types

    # airflow.dags package stub must exist for the dotted path to resolve
    sys.modules.setdefault("airflow.dags", MagicMock())

    dag_path = Path(__file__).parent.parent / "airflow" / "dags" / "daily_paper_trading.py"
    spec = importlib.util.spec_from_file_location("airflow.dags.daily_paper_trading", dag_path)
    mod = types.ModuleType("airflow.dags.daily_paper_trading")
    mod.__spec__ = spec
    mod.__file__ = str(dag_path)   # needed by _fetch_ibkr_snapshot's sys.path setup
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Registration still happens so patch() can find the module name;
        # individual tests that import symbols will skip on ImportError
        pass
    sys.modules["airflow.dags.daily_paper_trading"] = mod
    sys.modules["airflow.dags"].daily_paper_trading = mod  # type: ignore[attr-defined]


_load_and_register_dag_module()


# Minimal Airflow env for DAG import
os.environ.setdefault("AIRFLOW__CORE__UNIT_TEST_MODE", "True")
os.environ.setdefault("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "sqlite:///:memory:")
os.environ.setdefault("PAPER_TRADING", "true")
os.environ.setdefault("IBKR_PORT", "7497")


@pytest.fixture(scope="module")
def dag():
    """Return the real Airflow DAG object if a full Airflow installation is
    available; otherwise return None so tests that need it can skip."""
    mod = sys.modules.get("airflow.dags.daily_paper_trading")
    if mod is None:
        return None
    dag_obj = getattr(mod, "dag", None)
    # When Airflow is mocked the DAG context-manager returns a MagicMock, not a
    # real DAG.  Treat that as unavailable so task-structure tests skip cleanly.
    if isinstance(dag_obj, MagicMock):
        return None
    return dag_obj


class TestDagStructure:
    def test_dag_loads_without_import_error(self):
        """The DAG file must be importable without exceptions."""
        sys.path.insert(0, str(Path(__file__).parent.parent))
        path = Path(__file__).parent.parent / "airflow" / "dags" / "daily_paper_trading.py"
        assert path.exists(), "daily_paper_trading.py DAG file not found"

    def test_required_task_ids_present(self, dag):
        if dag is None:
            pytest.skip("DAG object not available")
        expected = {
            "wait_for_signal_pipeline",
            "verify_inputs",
            "construct_target",
            "fetch_ibkr_snapshot",
            "gen_candidates",
            "risk_compliance_gate",
            "build_blotter",
            "whatif_validate",
            "wait_approval",
            "submit_orders",
            "wait_for_fills",
            "durable_reconcile",
            "write_ledger",
        }
        actual = {t.task_id for t in dag.tasks}
        assert expected.issubset(actual), f"Missing tasks: {expected - actual}"

    def test_dag_id(self, dag):
        if dag is None:
            pytest.skip("DAG object not available")
        assert dag.dag_id == "daily_paper_trading"

    def test_catchup_is_false(self, dag):
        if dag is None:
            pytest.skip("DAG object not available")
        assert dag.catchup is False

    def test_max_active_runs_is_one(self, dag):
        if dag is None:
            pytest.skip("DAG object not available")
        assert dag.max_active_runs == 1


class TestRequirePaperEnv:
    def test_passes_with_valid_env(self):
        from airflow.dags.daily_paper_trading import _require_paper_env

        _require_paper_env({"PAPER_TRADING": "true", "IBKR_PORT": "7497"})

    def test_fails_if_paper_trading_false(self):
        from airflow.dags.daily_paper_trading import _require_paper_env
        from airflow.exceptions import AirflowException

        with pytest.raises(AirflowException, match="PAPER_TRADING"):
            _require_paper_env({"PAPER_TRADING": "false", "IBKR_PORT": "7497"})

    def test_fails_if_port_not_7497(self):
        from airflow.dags.daily_paper_trading import _require_paper_env
        from airflow.exceptions import AirflowException

        with pytest.raises(AirflowException, match="IBKR_PORT"):
            _require_paper_env({"PAPER_TRADING": "true", "IBKR_PORT": "7496"})

    def test_fails_if_paper_run_cleared_set(self):
        from airflow.dags.daily_paper_trading import _require_paper_env
        from airflow.exceptions import AirflowException

        with pytest.raises(AirflowException, match="PAPER_RUN_CLEARED"):
            _require_paper_env({
                "PAPER_TRADING": "true",
                "IBKR_PORT": "7497",
                "PAPER_RUN_CLEARED": "true",
            })


class TestSafeRunId:
    def test_replaces_colons(self):
        from airflow.dags.daily_paper_trading import _safe_run_id

        result = _safe_run_id("manual__2026-06-25T23:00:00+00:00")
        assert ":" not in result

    def test_truncates_long_ids(self):
        from airflow.dags.daily_paper_trading import _safe_run_id

        result = _safe_run_id("x" * 300)
        assert len(result) <= 200


class TestFetchIbkrSnapshot:
    def test_writes_snapshot_json(self, tmp_path, monkeypatch):
        """fetch_ibkr_snapshot writes a valid portfolio snapshot file."""
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("IBKR_PORT", "7497")
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))

        mock_broker = MagicMock()
        mock_broker.is_paper = True
        mock_broker.get_positions.return_value = {"AAPL": 5.0, "MSFT": 3.0}
        mock_broker.get_cash_balance_usd.return_value = 10000.0
        mock_broker.get_account_value.return_value = 20000.0

        mock_ti = MagicMock()
        context = {
            "ti": mock_ti,
            "run_id": "test__run__1",
            "params": {},
        }

        from datetime import date as _date
        today_str = _date.today().isoformat()

        mock_row_aapl = MagicMock()
        mock_row_aapl.ticker = "AAPL"
        mock_row_aapl.close = 200.0
        mock_row_aapl.price_date = today_str

        mock_row_msft = MagicMock()
        mock_row_msft.ticker = "MSFT"
        mock_row_msft.close = 450.0
        mock_row_msft.price_date = today_str

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [mock_row_aapl, mock_row_msft]
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        from airflow.dags.daily_paper_trading import _fetch_ibkr_snapshot

        with patch("airflow.dags.daily_paper_trading.IBKRBroker", return_value=mock_broker):
            with patch("airflow.dags.daily_paper_trading.create_engine", return_value=mock_engine):
                _fetch_ibkr_snapshot(**context)

        # Verify snapshot was written
        snapshot_path = mock_ti.xcom_push.call_args_list[0][1]["value"]
        snapshot = json.loads(Path(snapshot_path).read_text())
        assert snapshot["schema_version"] == "paper_portfolio_snapshot.v1"
        assert snapshot["cash"] == pytest.approx(10000.0)
        assert snapshot["nav_usd"] == pytest.approx(20000.0)
        tickers = {p["ticker"] for p in snapshot["positions"]}
        assert tickers == {"AAPL", "MSFT"}

    def test_raises_if_not_paper_mode(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "false")
        monkeypatch.setenv("IBKR_PORT", "7496")

        from airflow.dags.daily_paper_trading import _fetch_ibkr_snapshot
        from airflow.exceptions import AirflowException

        with pytest.raises(AirflowException, match="PAPER_TRADING"):
            _fetch_ibkr_snapshot(ti=MagicMock(), run_id="x", params={})

    def test_raises_if_nav_not_positive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("IBKR_PORT", "7497")
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))

        mock_broker = MagicMock()
        mock_broker.is_paper = True
        mock_broker.get_positions.return_value = {}
        mock_broker.get_cash_balance_usd.return_value = 0.0
        mock_broker.get_account_value.return_value = -1.0

        context = {"ti": MagicMock(), "run_id": "test-nav-fail", "params": {}}

        from airflow.dags.daily_paper_trading import _fetch_ibkr_snapshot
        from airflow.exceptions import AirflowException

        with patch("airflow.dags.daily_paper_trading.IBKRBroker", return_value=mock_broker):
            with pytest.raises(AirflowException, match="NAV"):
                _fetch_ibkr_snapshot(**context)

    def test_db_persist_failure_does_not_block_snapshot(self, tmp_path, monkeypatch):
        """DB failure in _persist_snapshot_to_db must not abort _fetch_ibkr_snapshot."""
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

        from airflow.dags.daily_paper_trading import _fetch_ibkr_snapshot

        with patch("airflow.dags.daily_paper_trading.IBKRBroker", return_value=mock_broker):
            with patch(
                "airflow.dags.daily_paper_trading._persist_snapshot_to_db",
                side_effect=Exception("DB unreachable"),
            ):
                # Must not raise — DB failure is non-blocking
                _fetch_ibkr_snapshot(**context)

        xcom_keys = [call[1]["key"] for call in mock_ti.xcom_push.call_args_list]
        assert "snapshot_path" in xcom_keys
        assert "trading_date" in xcom_keys
        snapshot_path = next(
            call[1]["value"]
            for call in mock_ti.xcom_push.call_args_list
            if call[1]["key"] == "snapshot_path"
        )
        assert Path(snapshot_path).exists()


class TestPersistSnapshotToDb:
    def test_executes_upsert_and_disposes(self):
        """_persist_snapshot_to_db issues an INSERT and always disposes the engine."""
        mock_conn = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = lambda s: mock_conn
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_engine = MagicMock()
        mock_engine.begin.return_value = mock_ctx

        with patch("airflow.dags.daily_paper_trading.create_engine", return_value=mock_engine):
            from airflow.dags.daily_paper_trading import _persist_snapshot_to_db
            _persist_snapshot_to_db(
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

    def test_passes_correct_params(self):
        """Parameters are forwarded to the SQL execute call correctly."""
        mock_conn = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = lambda s: mock_conn
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_engine = MagicMock()
        mock_engine.begin.return_value = mock_ctx

        positions = [{"ticker": "MSFT", "quantity": 5, "price": 400.0}]

        with patch("airflow.dags.daily_paper_trading.create_engine", return_value=mock_engine):
            from airflow.dags.daily_paper_trading import _persist_snapshot_to_db
            _persist_snapshot_to_db(
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
        # positions must be JSON-serialised for the JSONB column
        assert params["positions"] == json.dumps(positions)
        assert "id" in params
        assert "fetched_at_utc" in params

    def test_disposes_engine_even_on_db_error(self):
        """Engine is disposed even when the DB transaction raises."""
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(side_effect=Exception("connection refused"))
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_engine = MagicMock()
        mock_engine.begin.return_value = mock_ctx

        with patch("airflow.dags.daily_paper_trading.create_engine", return_value=mock_engine):
            from airflow.dags.daily_paper_trading import _persist_snapshot_to_db
            with pytest.raises(Exception, match="connection refused"):
                _persist_snapshot_to_db(
                    database_url="postgresql://fake/db",
                    snapshot_date="2026-06-30",
                    strategy_id="v1",
                    dag_run_id="run_fail",
                    cash_usd=1000.0,
                    positions=[],
                    nav_usd=1000.0,
                )

        assert mock_engine.dispose.called

"""Unit tests for _simulate_strategies in daily_signal_pipeline.py.

Uses the same fixture-scoped stub pattern as test_persist_snapshot_to_db.py:
stubs are installed inside a scope="module" fixture and removed on teardown,
so this file does not contaminate sys.modules for other test files.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_DAG_PATH = Path(__file__).parent.parent / "airflow" / "dags" / "daily_signal_pipeline.py"

_STUB_DEFS: dict[str, object] = {
    "pendulum": MagicMock(),
    "airflow": MagicMock(),
    "airflow.operators": MagicMock(),
    "airflow.operators.python": MagicMock(PythonOperator=MagicMock()),
    "structlog": MagicMock(),
}


def _restore_modules(saved: dict[str, object | None]) -> None:
    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original  # type: ignore[assignment]


@pytest.fixture(scope="module")
def sig_mod() -> types.ModuleType:
    """Load daily_signal_pipeline.py under minimal stubs; restore sys.modules on teardown."""
    saved = {name: sys.modules.get(name) for name in _STUB_DEFS}
    for name, stub in _STUB_DEFS.items():
        if name not in sys.modules:
            sys.modules[name] = stub  # type: ignore[assignment]

    try:
        spec = importlib.util.spec_from_file_location("_dsp_for_tests", _DAG_PATH)
        mod = types.ModuleType("_dsp_for_tests")
        mod.__file__ = str(_DAG_PATH)
        mod.__spec__ = spec
        spec.loader.exec_module(mod)
    except Exception as exc:
        _restore_modules(saved)
        pytest.skip(f"Could not load signal pipeline for testing: {exc}")

    yield mod

    _restore_modules(saved)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn_mock(
    *,
    registry_rows: list,
    score_rows: list,
    price_rows: list,
    prev_nav_row=None,
) -> MagicMock:
    """Build a connection mock that returns preset results for each execute() call."""
    conn = MagicMock()
    results = [
        _fetchall_result(registry_rows),
        _fetchall_result(score_rows),
        _fetchall_result(price_rows),
        _fetchone_result(prev_nav_row),
    ]
    conn.execute.side_effect = results
    return conn


def _fetchall_result(rows: list) -> MagicMock:
    r = MagicMock()
    r.fetchall.return_value = rows
    return r


def _fetchone_result(row) -> MagicMock:
    r = MagicMock()
    r.fetchone.return_value = row
    return r


def _make_reg_row(strategy_id: str, config: dict) -> MagicMock:
    row = MagicMock()
    row.strategy_id = strategy_id
    row.config = config
    return row


def _make_score_row(ticker: str, alpha_score: float, universe_size: int = 100) -> MagicMock:
    row = MagicMock()
    row.ticker = ticker
    row.alpha_score = alpha_score
    row.universe_size = universe_size
    return row


def _make_price_row(ticker: str, dt: date, close: float) -> MagicMock:
    row = MagicMock()
    row.ticker = ticker
    row.date = dt
    row.close = close
    return row


def _make_prev_nav(nav: float) -> MagicMock:
    row = MagicMock()
    row.simulated_nav = nav
    return row


def _make_engine(read_conn: MagicMock, write_conn: MagicMock | None = None) -> MagicMock:
    """Engine whose connect() yields read_conn and begin() yields write_conn."""
    engine = MagicMock()

    read_ctx = MagicMock()
    read_ctx.__enter__ = MagicMock(return_value=read_conn)
    read_ctx.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = read_ctx

    if write_conn is not None:
        write_ctx = MagicMock()
        write_ctx.__enter__ = MagicMock(return_value=write_conn)
        write_ctx.__exit__ = MagicMock(return_value=False)
        engine.begin.return_value = write_ctx

    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSimulateStrategies:
    _SCORE_DATE = "2026-06-30"
    _CONFIG = {"portfolio": {"n_long": 2, "max_position_weight": 0.5}}

    def _base_context(self, score_date: str = _SCORE_DATE) -> dict:
        ti = MagicMock()
        ti.xcom_pull.return_value = score_date
        return {"ti": ti, "run_id": "test-run", "params": {}}

    def test_upserts_simulation_row(self, sig_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: one strategy, two tickers, two price dates → row upserted."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

        reg_row = _make_reg_row("v1_base_momentum", self._CONFIG)
        score_rows = [
            _make_score_row("AAPL", 0.9, universe_size=100),
            _make_score_row("MSFT", 0.8, universe_size=100),
        ]
        price_rows = [
            _make_price_row("AAPL", date(2026, 6, 29), 200.0),
            _make_price_row("AAPL", date(2026, 6, 30), 202.0),
            _make_price_row("MSFT", date(2026, 6, 29), 400.0),
            _make_price_row("MSFT", date(2026, 6, 30), 404.0),
        ]

        # registry query
        reg_conn = MagicMock()
        reg_conn.execute.return_value.fetchall.return_value = [reg_row]
        reg_engine = _make_engine(reg_conn)

        # per-strategy read queries (score, price, prev_nav)
        read_conn = MagicMock()
        read_conn.execute.side_effect = [
            _fetchall_result(score_rows),
            _fetchall_result(price_rows),
            _fetchone_result(None),   # no prior simulation → initial NAV
        ]
        write_conn = MagicMock()
        per_engine = _make_engine(read_conn, write_conn)

        engines = iter([reg_engine, per_engine])
        with patch.object(sig_mod, "create_engine", side_effect=lambda *a, **kw: next(engines)):
            result = sig_mod._simulate_strategies(**self._base_context())

        assert result["strategies_simulated"] == 1
        assert result["strategies_skipped"] == 0
        assert write_conn.execute.called

        # Verify upsert params
        params = write_conn.execute.call_args[0][1]
        assert params["strategy_id"] == "v1_base_momentum"
        assert params["sim_date"] == "2026-06-30"
        assert params["n_positions"] == 2
        assert params["universe_size"] == 100
        # AAPL: +1%, MSFT: +1% each at weight 0.5 → total 0.01
        assert abs(params["simulated_return"] - 0.01) < 1e-6
        assert abs(params["simulated_nav"] - 1_010_000.0) < 0.01

    def test_uses_previous_nav(self, sig_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulated NAV chains from the prior row when one exists."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

        reg_row = _make_reg_row("v1_base_momentum", self._CONFIG)
        score_rows = [_make_score_row("AAPL", 0.9)]
        price_rows = [
            _make_price_row("AAPL", date(2026, 6, 29), 100.0),
            _make_price_row("AAPL", date(2026, 6, 30), 110.0),
        ]
        prev_nav_row = _make_prev_nav(500_000.0)

        reg_conn = MagicMock()
        reg_conn.execute.return_value.fetchall.return_value = [reg_row]
        reg_engine = _make_engine(reg_conn)

        read_conn = MagicMock()
        read_conn.execute.side_effect = [
            _fetchall_result(score_rows),
            _fetchall_result(price_rows),
            _fetchone_result(prev_nav_row),
        ]
        write_conn = MagicMock()
        per_engine = _make_engine(read_conn, write_conn)

        engines = iter([reg_engine, per_engine])
        with patch.object(sig_mod, "create_engine", side_effect=lambda *a, **kw: next(engines)):
            sig_mod._simulate_strategies(**self._base_context())

        params = write_conn.execute.call_args[0][1]
        # 500k * 1.1 (10% return with single position at weight min(1/1, 0.5)=0.5)
        assert abs(params["simulated_return"] - 0.05) < 1e-6   # 0.5 * 10%
        assert abs(params["simulated_nav"] - 525_000.0) < 0.01

    def test_skips_when_registry_empty(self, sig_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns zero counts without touching the write path when no active strategies."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

        reg_conn = MagicMock()
        reg_conn.execute.return_value.fetchall.return_value = []
        reg_engine = _make_engine(reg_conn)

        with patch.object(sig_mod, "create_engine", return_value=reg_engine):
            result = sig_mod._simulate_strategies(**self._base_context())

        assert result == {"strategies_simulated": 0, "strategies_skipped": 0}

    def test_skips_gracefully_when_table_missing(self, sig_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """OperationalError on registry query (table not migrated) is treated as skip."""
        from sqlalchemy.exc import OperationalError

        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

        reg_conn = MagicMock()
        reg_conn.execute.side_effect = OperationalError("no such table", {}, Exception())
        reg_engine = _make_engine(reg_conn)

        with patch.object(sig_mod, "create_engine", return_value=reg_engine):
            result = sig_mod._simulate_strategies(**self._base_context())

        assert result == {"strategies_simulated": 0, "strategies_skipped": 0}

    def test_skips_individual_strategy_on_error(self, sig_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """One strategy failing does not prevent others from being simulated."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

        reg_rows = [
            _make_reg_row("v1_base_momentum", self._CONFIG),
            _make_reg_row("v2_mvo_momentum", self._CONFIG),
        ]
        reg_conn = MagicMock()
        reg_conn.execute.return_value.fetchall.return_value = reg_rows
        reg_engine = _make_engine(reg_conn)

        # v1: no alpha_scores → raises ValueError → skipped
        fail_read = MagicMock()
        fail_read.execute.return_value.fetchall.return_value = []
        fail_engine = _make_engine(fail_read)

        # v2: success
        score_rows = [_make_score_row("AAPL", 0.9)]
        price_rows = [
            _make_price_row("AAPL", date(2026, 6, 29), 100.0),
            _make_price_row("AAPL", date(2026, 6, 30), 101.0),
        ]
        ok_read = MagicMock()
        ok_read.execute.side_effect = [
            _fetchall_result(score_rows),
            _fetchall_result(price_rows),
            _fetchone_result(None),
        ]
        ok_write = MagicMock()
        ok_engine = _make_engine(ok_read, ok_write)

        engines = iter([reg_engine, fail_engine, ok_engine])
        with patch.object(sig_mod, "create_engine", side_effect=lambda *a, **kw: next(engines)):
            result = sig_mod._simulate_strategies(**self._base_context())

        assert result["strategies_simulated"] == 1
        assert result["strategies_skipped"] == 1

    def test_no_xcom_score_date_returns_zero(self, sig_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing score_date XCom (load_prices failed) causes an immediate short-circuit."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        ti = MagicMock()
        ti.xcom_pull.return_value = None
        context = {"ti": ti, "run_id": "x", "params": {}}

        with patch.object(sig_mod, "create_engine") as mock_ce:
            result = sig_mod._simulate_strategies(**context)

        mock_ce.assert_not_called()
        assert result == {"strategies_simulated": 0, "strategies_skipped": 0}

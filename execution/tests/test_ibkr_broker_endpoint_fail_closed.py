"""BUG-004 failure-path tests: broker endpoint validation must stop before
any DAG task can reach the order-submission path (Gate 01A, Phase 4).

Covers the two failure paths the checklist calls out explicitly:
  - an unavailable/unresolvable hostname or refused socket at connect() time
  - IBKR_PORT=7496 (live) reached without the full C8/C9 clearance sequence

Both must raise before IBKRBroker ever reaches a state a caller could use to
submit an order, and both must propagate out of the DAG's
`_fetch_ibkr_snapshot` task (the first task that touches the broker) rather
than being swallowed -- Airflow's default trigger rule then skips every
downstream task, including `submit_orders`, so a broker failure here can
never fall through to a live submission attempt.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from execution.brokers.ibkr import IBKRBroker

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def _clean_broker_env(monkeypatch):
    monkeypatch.delenv("RQIS_RUNTIME_CONTEXT", raising=False)
    monkeypatch.delenv("RQIS_RUNTIME_NETWORK_MODE", raising=False)
    monkeypatch.delenv("PAPER_RUN_CLEARED", raising=False)


class TestLivePortRejectedBeforeConnection:
    """IBKR_PORT=7496 must never construct a usable broker without the full
    C8/C9 clearance chain, regardless of runtime context."""

    def test_live_port_without_paper_trading_false_fails_closed(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "true")  # default/unset also covered below
        monkeypatch.setenv("IBKR_PORT", "7496")
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        with pytest.raises(OSError, match="PAPER_TRADING=false to be explicitly set"):
            IBKRBroker()

    def test_live_port_with_unset_paper_trading_fails_closed(self, monkeypatch):
        monkeypatch.delenv("PAPER_TRADING", raising=False)
        monkeypatch.setenv("IBKR_PORT", "7496")
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        with pytest.raises(OSError, match="PAPER_TRADING=false to be explicitly set"):
            IBKRBroker()

    def test_live_port_with_paper_trading_false_but_not_cleared_fails_closed(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "false")
        monkeypatch.setenv("IBKR_PORT", "7496")
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        monkeypatch.delenv("PAPER_RUN_CLEARED", raising=False)
        with pytest.raises(OSError, match="PAPER_RUN_CLEARED=true"):
            IBKRBroker()

    def test_live_port_inside_bridged_context_still_fails_closed(self, monkeypatch):
        # The bridged-host guard and the paper/live guard are independent
        # defense-in-depth checks; a live port must fail even when the host
        # is otherwise valid inside a Compose container.
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("IBKR_PORT", "7496")
        monkeypatch.setenv("IBKR_HOST", "host.docker.internal")
        with pytest.raises(OSError, match="PAPER_TRADING=false to be explicitly set"):
            IBKRBroker()

    def test_connect_revalidates_live_port_even_if_env_changed_after_init(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("IBKR_PORT", "7497")
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        broker = IBKRBroker()

        monkeypatch.setenv("IBKR_PORT", "7496")
        broker._port = 7496  # simulate a long-lived object whose port env drifted
        with pytest.raises(OSError, match="PAPER_TRADING=false to be explicitly set"):
            broker.connect()


class TestUnresolvableHostFailsClosedBeforeSubmission:
    """A broker that cannot reach TWS/IB Gateway must raise out of connect(),
    and that exception must propagate out of the DAG's first broker-touching
    task rather than being swallowed."""

    def test_connect_propagates_socket_failure(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("IBKR_PORT", "7497")
        monkeypatch.setenv("IBKR_HOST", "this-host-does-not-resolve.invalid")
        broker = IBKRBroker()

        class _RefusingIB:
            def isConnected(self):
                return False

            def connect(self, host, port, clientId, timeout):
                raise ConnectionRefusedError(
                    f"could not connect to {host}:{port} (simulated unresolvable host)"
                )

        with patch("execution.brokers.ibkr.IB", return_value=_RefusingIB()):
            with pytest.raises(ConnectionRefusedError):
                broker.connect()

    def test_dag_fetch_ibkr_snapshot_task_propagates_connect_failure(self, monkeypatch):
        """Integration-style: the DAG task must not catch-and-continue past a
        broker connection failure -- it must raise, which is what causes
        Airflow to skip every downstream task including submit_orders."""
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("IBKR_PORT", "7497")
        monkeypatch.setenv("IBKR_HOST", "this-host-does-not-resolve.invalid")
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

        mock_broker = MagicMock()
        mock_broker.is_paper = True
        mock_broker.connect.side_effect = ConnectionRefusedError("simulated unresolvable host")

        from airflow.dags.daily_paper_trading import _fetch_ibkr_snapshot

        context = {"ti": MagicMock(), "run_id": "test-unresolvable-host", "params": {}}
        with patch("airflow.dags.daily_paper_trading.IBKRBroker", return_value=mock_broker):
            with pytest.raises(ConnectionRefusedError):
                _fetch_ibkr_snapshot(**context)

        # The order-submission path must never have been reached.
        mock_broker.get_positions.assert_not_called()
        mock_broker.get_cash_balance_usd.assert_not_called()
        mock_broker.get_account_value.assert_not_called()

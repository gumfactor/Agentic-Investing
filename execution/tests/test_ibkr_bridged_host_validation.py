"""Fail-closed tests for BUG-004: IBKR_HOST must not default to a loopback
address inside a bridged Docker Compose runtime.

execution/brokers/ibkr.py._validate_bridged_broker_host() is the production
guard (not a test-only shim): it is called from both IBKRBroker.__init__ and
IBKRBroker.connect(), and it only activates when RQIS_RUNTIME_CONTEXT is set
to "compose_bridged" -- the marker every Airflow Compose service sets (see
docker-compose.yml x-airflow-common). Host-side operator CLI scripts never
set that marker, so 127.0.0.1 continues to work there.
"""

from __future__ import annotations

import pytest

from execution.brokers.ibkr import IBKRBroker, _validate_bridged_broker_host


@pytest.fixture(autouse=True)
def _paper_env(monkeypatch):
    # Keep every case in this file paper-mode / paper-port per repo guardrails.
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.delenv("PAPER_RUN_CLEARED", raising=False)


class TestValidateBridgedBrokerHostDirect:
    """Unit tests against the guard function itself."""

    @pytest.mark.parametrize("host", ["", "127.0.0.1", "localhost", "::1", "0.0.0.0", None])
    def test_rejects_loopback_hosts_in_bridged_context(self, monkeypatch, host):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        with pytest.raises(OSError, match="not reachable from a bridged Docker Compose network"):
            _validate_bridged_broker_host(host)

    @pytest.mark.parametrize("host", ["", "127.0.0.1", "localhost", None])
    def test_allows_loopback_hosts_outside_bridged_context(self, monkeypatch, host):
        monkeypatch.delenv("RQIS_RUNTIME_CONTEXT", raising=False)
        _validate_bridged_broker_host(host)  # must not raise

    def test_allows_docker_internal_host_in_bridged_context(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        _validate_bridged_broker_host("host.docker.internal")  # must not raise

    def test_allows_explicit_gateway_ip_in_bridged_context(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        _validate_bridged_broker_host("172.17.0.1")  # must not raise

    def test_loopback_allowed_when_host_network_explicitly_declared(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        monkeypatch.setenv("RQIS_RUNTIME_NETWORK_MODE", "host")
        _validate_bridged_broker_host("127.0.0.1")  # must not raise -- declared escape hatch

    def test_bridged_context_value_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "Compose_Bridged")
        with pytest.raises(OSError):
            _validate_bridged_broker_host("127.0.0.1")


class TestIBKRBrokerConstructionFailsClosed:
    """Integration-style tests: an empty/unset IBKR_HOST must not silently
    fall back to IBKRBroker's own 127.0.0.1 default inside a bridged
    container -- this is the exact scenario BUG-004 describes."""

    def test_unset_ibkr_host_env_fails_closed_in_bridged_context(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        monkeypatch.delenv("IBKR_HOST", raising=False)
        with pytest.raises(OSError, match="BUG-004"):
            IBKRBroker()

    def test_empty_ibkr_host_env_fails_closed_in_bridged_context(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        monkeypatch.setenv("IBKR_HOST", "")
        with pytest.raises(OSError, match="BUG-004"):
            IBKRBroker()

    def test_localhost_ibkr_host_env_fails_closed_in_bridged_context(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        monkeypatch.setenv("IBKR_HOST", "localhost")
        with pytest.raises(OSError, match="BUG-004"):
            IBKRBroker()

    def test_explicit_loopback_constructor_arg_fails_closed_in_bridged_context(self, monkeypatch):
        # Even a caller-supplied host= argument (not just the env default)
        # must be rejected -- this guards against a hardcoded 127.0.0.1 call
        # site, not only a missing environment variable.
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        monkeypatch.setenv("IBKR_HOST", "host.docker.internal")
        with pytest.raises(OSError, match="BUG-004"):
            IBKRBroker(host="127.0.0.1")

    def test_docker_internal_host_constructs_successfully_in_bridged_context(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        monkeypatch.setenv("IBKR_HOST", "host.docker.internal")
        broker = IBKRBroker()
        assert broker._host == "host.docker.internal"

    def test_loopback_ibkr_host_still_allowed_outside_bridged_context(self, monkeypatch):
        # Host-side operator CLI scripts (no RQIS_RUNTIME_CONTEXT set) must be
        # unaffected -- 127.0.0.1 is the correct address there.
        monkeypatch.delenv("RQIS_RUNTIME_CONTEXT", raising=False)
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        broker = IBKRBroker()
        assert broker._host == "127.0.0.1"

    def test_connect_revalidates_host_if_env_changed_after_init(self, monkeypatch):
        # Mirrors the existing _validate_paper_trading_flag() re-check pattern:
        # env vars can change between __init__ and connect() (e.g. a long-lived
        # worker process), so connect() must re-run the bridged-host guard too.
        monkeypatch.delenv("RQIS_RUNTIME_CONTEXT", raising=False)
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        broker = IBKRBroker()

        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        with pytest.raises(OSError, match="BUG-004"):
            broker.connect()

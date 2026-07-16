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

    @pytest.mark.parametrize(
        "host",
        [
            "", "127.0.0.1", "localhost", "::1", "0.0.0.0", None,
            # Codex review fix: loopback ALIASES across the whole 127.0.0.0/8
            # range and shorthand spellings must also fail closed, not just
            # the canonical strings above.
            "127.0.1.1", "127.1.2.3", "127.255.255.254", "127.1",
            "::", "::1%eth0", "LOCALHOST", " 127.0.0.1 ",
        ],
    )
    def test_rejects_loopback_hosts_in_bridged_context(self, monkeypatch, host):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        with pytest.raises(OSError, match="not reachable from a containerized runtime"):
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

    @pytest.mark.parametrize(
        "context", ["compose-bridged", "kubernetes", "docker", "COMPOSE BRIDGED", "x"]
    )
    def test_unknown_nonempty_context_values_enforce_fail_closed(self, monkeypatch, context):
        """P2-2 (adversarial fix round): a typo or unreviewed deployment label
        must arm the guard, not silently deactivate it. Only an entirely
        unset/empty RQIS_RUNTIME_CONTEXT (host-side script) is a no-op."""
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", context)
        with pytest.raises(OSError, match="BUG-004"):
            _validate_bridged_broker_host("127.0.0.1")

    def test_unknown_context_still_allows_non_loopback_host(self, monkeypatch):
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose-bridged")
        _validate_bridged_broker_host("host.docker.internal")  # must not raise


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

    @pytest.mark.parametrize("host", ["127.0.1.1", "127.1"])
    def test_loopback_alias_ibkr_host_env_fails_closed_in_bridged_context(self, monkeypatch, host):
        """Codex review fix: 127/8 aliases the socket layer would happily
        connect to (the container's own loopback) must be rejected the same
        way as canonical 127.0.0.1."""
        monkeypatch.setenv("RQIS_RUNTIME_CONTEXT", "compose_bridged")
        monkeypatch.setenv("IBKR_HOST", host)
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


class TestClientIdFromEnv:
    """P1-2 (adversarial fix round): IBKR_CLIENT_ID is part of the Compose
    contract and must actually be consumed by IBKRBroker, not just declared
    in docker-compose.yml. Invalid values fail closed instead of silently
    becoming client id 1."""

    def test_client_id_defaults_to_1_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("IBKR_CLIENT_ID", raising=False)
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        broker = IBKRBroker()
        assert broker._client_id == 1

    def test_client_id_read_from_env(self, monkeypatch):
        monkeypatch.setenv("IBKR_CLIENT_ID", "7")
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        broker = IBKRBroker()
        assert broker._client_id == 7

    def test_explicit_constructor_arg_beats_env(self, monkeypatch):
        monkeypatch.setenv("IBKR_CLIENT_ID", "7")
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        broker = IBKRBroker(client_id=3)
        assert broker._client_id == 3

    @pytest.mark.parametrize("raw", ["abc", "1.5", "-2", " 7x "])
    def test_invalid_env_client_id_fails_closed(self, monkeypatch, raw):
        monkeypatch.setenv("IBKR_CLIENT_ID", raw)
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        with pytest.raises(OSError, match="IBKR_CLIENT_ID"):
            IBKRBroker()

    def test_empty_env_client_id_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("IBKR_CLIENT_ID", "")
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        broker = IBKRBroker()
        assert broker._client_id == 1

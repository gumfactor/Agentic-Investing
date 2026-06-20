"""Tests for the IBKR paper-readiness preflight command."""

from __future__ import annotations

from scripts import paper_readiness_check as check


class FakeBroker:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        timeout: int,
        positions: dict[str, float] | None = None,
        values: dict[str, float] | None = None,
        usd_nav: float = 740_000.0,
        paper: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.timeout = timeout
        self._positions = positions or {}
        self._values = values or {"CAD": 1_000_000.0}
        self._usd_nav = usd_nav
        self._paper = paper
        self.connected = False
        self.disconnected = False

    def connect(self) -> None:
        self.connected = True

    @property
    def is_paper(self) -> bool:
        return self._paper

    def get_positions(self) -> dict[str, float]:
        return self._positions

    def get_account_values_by_currency(self) -> dict[str, float]:
        return self._values

    def get_account_value(self) -> float:
        return self._usd_nav

    def disconnect(self) -> None:
        self.disconnected = True


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "PAPER_TRADING": "true",
        "IBKR_HOST": "127.0.0.1",
        "IBKR_PORT": "7497",
        "IBKR_CLIENT_ID": "7",
    }
    env.update(overrides)
    return env


def _socket_ok(_host: str, _port: int, _timeout: int, _recorder: check.CheckRecorder) -> bool:
    return True


def _socket_closed(_host: str, _port: int, _timeout: int, recorder: check.CheckRecorder) -> bool:
    recorder.fail("socket closed")
    return False


def test_run_passes_when_env_socket_and_broker_are_ready(capsys):
    created = []

    def broker_factory(**kwargs):
        broker = FakeBroker(**kwargs, positions={"AAPL": 2.0})
        created.append(broker)
        return broker

    result = check.run(
        argv=[],
        env=_env(),
        broker_factory=broker_factory,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper readiness: OK" in out
    assert "Positions: {AAPL: 2}" in out
    assert "USD-equivalent NAV: 740,000.00" in out
    assert created[0].host == "127.0.0.1"
    assert created[0].port == 7497
    assert created[0].client_id == 7
    assert created[0].disconnected


def test_run_fails_for_live_trading_env(capsys):
    result = check.run(
        argv=[],
        env=_env(PAPER_TRADING="false"),
        broker_factory=FakeBroker,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "PAPER_TRADING must be true" in out
    assert "Paper readiness: FAILED" in out


def test_run_fails_when_paper_trading_env_is_missing(capsys):
    env = _env()
    del env["PAPER_TRADING"]

    result = check.run(
        argv=[],
        env=env,
        broker_factory=FakeBroker,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "PAPER_TRADING must be set explicitly" in out


def test_run_fails_for_live_port(capsys):
    result = check.run(
        argv=[],
        env=_env(IBKR_PORT="7496"),
        broker_factory=FakeBroker,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "IBKR_PORT must be 7497" in out


def test_run_fails_when_ibkr_port_env_is_missing(capsys):
    env = _env()
    del env["IBKR_PORT"]

    result = check.run(
        argv=[],
        env=env,
        broker_factory=FakeBroker,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "IBKR_PORT must be set explicitly" in out


def test_run_fails_when_live_clearance_flag_is_enabled(capsys):
    result = check.run(
        argv=[],
        env=_env(PAPER_RUN_CLEARED="true"),
        broker_factory=FakeBroker,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "PAPER_RUN_CLEARED=true" in out


def test_run_fails_without_broker_when_socket_is_closed(capsys):
    calls = []

    def broker_factory(**_kwargs):
        calls.append("called")
        return FakeBroker(**_kwargs)

    result = check.run(
        argv=[],
        env=_env(),
        broker_factory=broker_factory,
        socket_checker=_socket_closed,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "socket closed" in out
    assert calls == []


def test_run_fails_when_broker_is_not_paper(capsys):
    def broker_factory(**kwargs):
        return FakeBroker(**kwargs, paper=False)

    result = check.run(
        argv=[],
        env=_env(),
        broker_factory=broker_factory,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "is_paper=False" in out


def test_run_reports_broker_nav_failure(capsys):
    class BrokerWithNavFailure(FakeBroker):
        def get_account_value(self) -> float:
            raise RuntimeError("manual FX rate is stale")

    result = check.run(
        argv=[],
        env=_env(),
        broker_factory=BrokerWithNavFailure,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "manual FX rate is stale" in out


def test_run_rejects_non_finite_usd_nav(capsys):
    def broker_factory(**kwargs):
        return FakeBroker(**kwargs, usd_nav=float("nan"))

    result = check.run(
        argv=[],
        env=_env(),
        broker_factory=broker_factory,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "USD-equivalent NAV must be finite and positive" in out


def test_run_rejects_non_positive_usd_nav(capsys):
    def broker_factory(**kwargs):
        return FakeBroker(**kwargs, usd_nav=0.0)

    result = check.run(
        argv=[],
        env=_env(),
        broker_factory=broker_factory,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "USD-equivalent NAV must be finite and positive" in out


def test_run_reports_broker_constructor_failure(capsys):
    def broker_factory(**_kwargs):
        raise RuntimeError("live port blocked")

    result = check.run(
        argv=[],
        env=_env(),
        broker_factory=broker_factory,
        socket_checker=_socket_ok,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "live port blocked" in out


def test_load_config_rejects_invalid_port():
    try:
        check.load_config(_env(IBKR_PORT="paper"), timeout=10, client_id=None)
    except RuntimeError as exc:
        assert "IBKR_PORT must be an integer" in str(exc)
    else:
        raise AssertionError("Expected invalid port to fail")


def test_format_positions_sorts_tickers():
    assert check.format_positions({"MSFT": 1.5, "AAPL": 2.0}) == "{AAPL: 2, MSFT: 1.5}"

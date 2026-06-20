"""Preflight IBKR paper-trading readiness without submitting orders.

Usage:
    python -m scripts.paper_readiness_check

This command verifies the local paper broker path only:
- environment is configured for paper mode
- TWS/Gateway socket is reachable
- IBKRBroker connects to paper port 7497
- positions and account NAV can be read
- USD-equivalent NAV conversion is available and fresh enough
"""

from __future__ import annotations

import argparse
import math
import os
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.brokers.ibkr import IBKRBroker


PAPER_PORT = 7497


@dataclass(frozen=True)
class ReadinessConfig:
    host: str
    port: int
    client_id: int
    timeout: int
    paper_trading: str | None
    paper_run_cleared: str


class CheckRecorder:
    def __init__(self) -> None:
        self.issues: list[str] = []

    def ok(self, message: str) -> None:
        print(f"OK: {message}")

    def fail(self, message: str) -> None:
        self.issues.append(message)
        print(f"FAIL: {message}")

    def info(self, message: str) -> None:
        print(f"INFO: {message}")

    @property
    def is_ok(self) -> bool:
        return not self.issues


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=10, help="Socket and broker connection timeout in seconds.")
    parser.add_argument("--client-id", type=int, default=None, help="Override IBKR client ID for this readiness check.")
    return parser.parse_args(argv)


def load_config(env: Mapping[str, str], timeout: int, client_id: int | None) -> ReadinessConfig:
    raw_port = env.get("IBKR_PORT")
    port = -1
    if raw_port is not None:
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise RuntimeError(f"IBKR_PORT must be an integer; got {raw_port!r}") from exc

    paper_trading = env.get("PAPER_TRADING")
    normalized_paper_trading = paper_trading.strip().lower() if paper_trading is not None else None

    raw_client_id = str(client_id if client_id is not None else env.get("IBKR_CLIENT_ID", "1"))
    try:
        parsed_client_id = int(raw_client_id)
    except ValueError as exc:
        raise RuntimeError(f"IBKR_CLIENT_ID must be an integer; got {raw_client_id!r}") from exc

    return ReadinessConfig(
        host=env.get("IBKR_HOST", "127.0.0.1"),
        port=port,
        client_id=parsed_client_id,
        timeout=timeout,
        paper_trading=normalized_paper_trading,
        paper_run_cleared=env.get("PAPER_RUN_CLEARED", "false").strip().lower(),
    )


def check_env(config: ReadinessConfig, recorder: CheckRecorder) -> bool:
    ok = True
    if config.paper_trading is None:
        recorder.fail("PAPER_TRADING must be set explicitly to true for paper readiness")
        ok = False
    elif config.paper_trading == "true":
        recorder.ok("PAPER_TRADING=true")
    else:
        recorder.fail(f"PAPER_TRADING must be true for paper readiness; got {config.paper_trading!r}")
        ok = False

    if config.port == -1:
        recorder.fail(f"IBKR_PORT must be set explicitly to {PAPER_PORT} for paper readiness")
        ok = False
    elif config.port == PAPER_PORT:
        recorder.ok(f"IBKR_PORT={PAPER_PORT}")
    else:
        recorder.fail(f"IBKR_PORT must be {PAPER_PORT} for paper readiness; got {config.port}")
        ok = False

    if config.paper_run_cleared == "true":
        recorder.fail("PAPER_RUN_CLEARED=true is a live-trading clearance flag; unset it for paper readiness checks")
        ok = False
    else:
        recorder.ok("PAPER_RUN_CLEARED is not enabled")

    return ok


def check_socket(host: str, port: int, timeout: int, recorder: CheckRecorder) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            recorder.ok(f"TWS/Gateway socket reachable at {host}:{port}")
            return True
    except OSError as exc:
        recorder.fail(f"TWS/Gateway socket is not reachable at {host}:{port}: {exc}")
        return False


def format_positions(positions: Mapping[str, float]) -> str:
    if not positions:
        return "{}"
    return "{" + ", ".join(f"{ticker}: {shares:g}" for ticker, shares in sorted(positions.items())) + "}"


def check_broker(
    config: ReadinessConfig,
    recorder: CheckRecorder,
    broker_factory: Callable[..., Any] = IBKRBroker,
) -> bool:
    broker = None
    try:
        broker = broker_factory(
            host=config.host,
            port=config.port,
            client_id=config.client_id,
            timeout=config.timeout,
        )
        broker.connect()
        if broker.is_paper:
            recorder.ok("IBKRBroker connected in paper mode")
        else:
            recorder.fail("IBKRBroker connected but is_paper=False")
            return False

        positions = broker.get_positions()
        values_by_currency = broker.get_account_values_by_currency()
        usd_nav = broker.get_account_value()
        if not math.isfinite(usd_nav) or usd_nav <= 0:
            recorder.fail(f"USD-equivalent NAV must be finite and positive; got {usd_nav!r}")
            return False

        recorder.info(f"Positions: {format_positions(positions)}")
        recorder.info(f"NAV by currency: {values_by_currency}")
        recorder.info(f"USD-equivalent NAV: {usd_nav:,.2f}")
        return True
    except Exception as exc:
        recorder.fail(f"IBKR broker readiness failed: {exc}")
        return False
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception as exc:
                recorder.fail(f"IBKR broker disconnect failed: {exc}")


def run(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    broker_factory: Callable[..., Any] = IBKRBroker,
    socket_checker: Callable[[str, int, int, CheckRecorder], bool] = check_socket,
) -> int:
    args = parse_args(argv)
    load_dotenv()
    env_map = os.environ if env is None else env
    recorder = CheckRecorder()

    try:
        config = load_config(env_map, timeout=args.timeout, client_id=args.client_id)
    except RuntimeError as exc:
        recorder.fail(str(exc))
        return 1

    recorder.info("IBKR paper readiness check")
    env_ok = check_env(config, recorder)
    socket_ok = socket_checker(config.host, config.port, config.timeout, recorder) if env_ok else False
    broker_ok = check_broker(config, recorder, broker_factory=broker_factory) if env_ok and socket_ok else False

    print()
    if recorder.is_ok and env_ok and socket_ok and broker_ok:
        print("Paper readiness: OK")
        return 0

    print("Paper readiness: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

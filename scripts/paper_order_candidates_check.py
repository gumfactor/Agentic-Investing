"""Generate paper-trading order candidates without staging orders.

Usage:
    python -m scripts.paper_order_candidates_check --strategy-id v1_base_momentum --portfolio-input portfolio.json

This Step 4 command is read-only and staging-free. It reuses the Step 3 target
portfolio path, reads current cash and positions from an explicit local JSON
snapshot, then prints weight-delta order candidates. It does not connect to
IBKR, instantiate OMS orders, run compliance/risk gates, stage orders, submit
orders, cancel orders, or reconcile fills.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.engine.fill_simulator import compute_orders
from scripts.paper_inputs_check import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_STRATEGY_CONFIG,
    CheckRecorder,
    _finite_number,
    load_strategy_config,
    resolve_strategy_id,
)
from scripts.paper_target_check import TargetPortfolio, construct_target_portfolio

DEFAULT_MIN_DELTA_WEIGHT = 1e-4
DEFAULT_MAX_SNAPSHOT_AGE_DAYS = 1


@dataclass(frozen=True)
class CurrentPosition:
    ticker: str
    quantity: float
    price: float

    @property
    def market_value(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True)
class PortfolioSnapshot:
    as_of: date
    cash: float
    positions: tuple[CurrentPosition, ...]

    @property
    def nav(self) -> float:
        return self.cash + sum(pos.market_value for pos in self.positions)

    @property
    def current_weights(self) -> dict[str, float]:
        nav = self.nav
        return {pos.ticker: pos.market_value / nav for pos in self.positions if pos.market_value > 0}


@dataclass(frozen=True)
class OrderCandidate:
    ticker: str
    direction: str
    current_weight: float
    target_weight: float
    delta_weight: float
    reference_price: float
    estimated_shares: float
    estimated_notional: float


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategy-config",
        type=Path,
        default=DEFAULT_STRATEGY_CONFIG,
        help=f"Strategy YAML path (default: {DEFAULT_STRATEGY_CONFIG}).",
    )
    parser.add_argument("--strategy-id", default=None, help="Database strategy_id to load from alpha_scores.")
    parser.add_argument(
        "--portfolio-input",
        type=Path,
        required=True,
        help="Local JSON snapshot with cash and positions; never read from IBKR in Step 4.",
    )
    parser.add_argument("--max-price-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--max-score-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--min-overlap", type=int, default=None)
    parser.add_argument(
        "--min-delta-weight",
        type=float,
        default=DEFAULT_MIN_DELTA_WEIGHT,
        help=f"Skip absolute weight deltas below this threshold (default: {DEFAULT_MIN_DELTA_WEIGHT}).",
    )
    parser.add_argument(
        "--max-snapshot-age-days",
        type=int,
        default=DEFAULT_MAX_SNAPSHOT_AGE_DAYS,
        help=(
            "Maximum calendar age for portfolio-input as_of date "
            f"(default: {DEFAULT_MAX_SNAPSHOT_AGE_DAYS})."
        ),
    )
    return parser.parse_args(argv)


def _parse_snapshot_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        raise RuntimeError("Portfolio input must include as_of date")
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise RuntimeError(f"Portfolio input as_of must be YYYY-MM-DD; got {value!r}") from exc


def _validate_snapshot_age(as_of: date, today: date, max_age_days: int) -> None:
    if max_age_days < 0:
        raise RuntimeError(f"--max-snapshot-age-days must be non-negative; got {max_age_days}")
    age_days = (today - as_of).days
    if age_days < 0:
        raise RuntimeError(f"Portfolio input as_of {as_of} is in the future relative to {today}")
    if age_days > max_age_days:
        raise RuntimeError(
            f"Portfolio input as_of {as_of} is stale: {age_days} days old, max {max_age_days}"
        )


def load_portfolio_snapshot(
    path: Path,
    *,
    today: date,
    max_snapshot_age_days: int,
) -> PortfolioSnapshot:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Portfolio input not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Portfolio input must be valid JSON: {path}") from exc

    if not isinstance(raw, Mapping):
        raise RuntimeError("Portfolio input must be a JSON object")

    as_of = _parse_snapshot_date(raw.get("as_of"))
    _validate_snapshot_age(as_of, today, max_snapshot_age_days)

    cash = _finite_number(raw.get("cash"))
    if cash is None or cash < 0:
        raise RuntimeError(f"Portfolio cash must be finite and non-negative; got {raw.get('cash')!r}")

    raw_positions = raw.get("positions", [])
    if not isinstance(raw_positions, list):
        raise RuntimeError("Portfolio positions must be a list")

    positions: list[CurrentPosition] = []
    seen: set[str] = set()
    for idx, row in enumerate(raw_positions, start=1):
        if not isinstance(row, Mapping):
            raise RuntimeError(f"Portfolio position #{idx} must be an object")
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            raise RuntimeError(f"Portfolio position #{idx} must include a ticker")
        if ticker in seen:
            raise RuntimeError(f"Duplicate portfolio position ticker: {ticker}")
        seen.add(ticker)

        quantity = _finite_number(row.get("quantity"))
        if quantity is None or quantity < 0:
            raise RuntimeError(f"Position {ticker} quantity must be finite and non-negative")
        price = _finite_number(row.get("price"))
        if price is None or price <= 0:
            raise RuntimeError(f"Position {ticker} price must be finite and positive")
        if quantity == 0:
            continue
        positions.append(CurrentPosition(ticker=ticker, quantity=quantity, price=price))

    snapshot = PortfolioSnapshot(
        as_of=as_of,
        cash=cash,
        positions=tuple(sorted(positions, key=lambda pos: pos.ticker)),
    )
    if not math.isfinite(snapshot.nav) or snapshot.nav <= 0:
        raise RuntimeError(f"Portfolio NAV must be finite and positive; got {snapshot.nav!r}")
    return snapshot


def build_order_candidates(
    *,
    target: TargetPortfolio,
    snapshot: PortfolioSnapshot,
    min_delta_weight: float,
) -> tuple[OrderCandidate, ...]:
    if not math.isfinite(min_delta_weight) or min_delta_weight < 0:
        raise RuntimeError(f"--min-delta-weight must be finite and non-negative; got {min_delta_weight!r}")

    target_weights = {pos.ticker.upper(): pos.target_weight for pos in target.positions}
    current_weights = snapshot.current_weights
    price_by_ticker = {pos.ticker.upper(): pos.latest_close for pos in target.positions}
    price_by_ticker.update({pos.ticker: pos.price for pos in snapshot.positions})

    candidates: list[OrderCandidate] = []
    for order in compute_orders(target_weights, current_weights, min_trade_weight=min_delta_weight):
        price = price_by_ticker.get(order.ticker)
        if price is None or not math.isfinite(price) or price <= 0:
            raise RuntimeError(f"Missing finite positive reference price for {order.ticker}")
        notional = abs(order.delta_weight) * snapshot.nav
        candidates.append(
            OrderCandidate(
                ticker=order.ticker,
                direction=order.direction,
                current_weight=order.current_weight,
                target_weight=order.target_weight,
                delta_weight=order.delta_weight,
                reference_price=price,
                estimated_shares=notional / price,
                estimated_notional=notional,
            )
        )
    return tuple(candidates)


def _print_candidates(
    *,
    target: TargetPortfolio,
    snapshot: PortfolioSnapshot,
    candidates: tuple[OrderCandidate, ...],
    min_delta_weight: float,
    recorder: CheckRecorder,
) -> None:
    recorder.info(
        f"Order candidates: strategy_id={target.strategy_id!r}, target_as_of_date={target.as_of_date}"
    )
    recorder.info(f"Portfolio snapshot as_of: {snapshot.as_of}")
    recorder.info(f"Current NAV from local snapshot: {snapshot.nav:.2f}")
    recorder.info(f"Current cash from local snapshot: {snapshot.cash:.2f}")
    recorder.info(f"Minimum delta weight: {min_delta_weight:.8f}")
    print(
        "ticker,direction,current_weight,target_weight,delta_weight,"
        "reference_price,estimated_shares,estimated_notional"
    )
    for candidate in candidates:
        print(
            f"{candidate.ticker},{candidate.direction},"
            f"{candidate.current_weight:.8f},{candidate.target_weight:.8f},"
            f"{candidate.delta_weight:.8f},{candidate.reference_price:.6f},"
            f"{candidate.estimated_shares:.6f},{candidate.estimated_notional:.2f}"
        )


def run(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    engine_factory: Callable[[str], Engine] = create_engine,
    today_fn: Callable[[], date] = date.today,
) -> int:
    args = parse_args(argv)
    load_dotenv()
    env_map = os.environ if env is None else env
    recorder = CheckRecorder()

    recorder.info("Paper order candidate check")
    database_url = env_map.get("DATABASE_URL")
    if not database_url:
        recorder.fail("DATABASE_URL must be set to construct paper order candidates")
        print()
        print("Paper order candidates: FAILED")
        return 1
    if not args.strategy_id:
        recorder.fail("--strategy-id must be passed explicitly for paper order candidate generation")
        print()
        print("Paper order candidates: FAILED")
        return 1

    candidates: tuple[OrderCandidate, ...] = ()
    snapshot: PortfolioSnapshot | None = None
    target: TargetPortfolio | None = None
    try:
        strategy_config = load_strategy_config(args.strategy_config)
        strategy_id = resolve_strategy_id(strategy_config, args.strategy_id)
        today = today_fn()
        snapshot = load_portfolio_snapshot(
            args.portfolio_input,
            today=today,
            max_snapshot_age_days=args.max_snapshot_age_days,
        )
        engine = engine_factory(database_url)
        target = construct_target_portfolio(
            engine=engine,
            strategy_config_path=args.strategy_config,
            strategy_config=strategy_config,
            strategy_id=strategy_id,
            max_price_age_days=args.max_price_age_days,
            max_score_age_days=args.max_score_age_days,
            min_overlap=args.min_overlap,
            today=today,
            recorder=recorder,
        )
        if target is not None:
            candidates = build_order_candidates(
                target=target,
                snapshot=snapshot,
                min_delta_weight=args.min_delta_weight,
            )
    except Exception as exc:
        recorder.fail(str(exc))

    print()
    if recorder.is_ok and target is not None and snapshot is not None:
        _print_candidates(
            target=target,
            snapshot=snapshot,
            candidates=candidates,
            min_delta_weight=args.min_delta_weight,
            recorder=recorder,
        )
        print("Paper order candidates: OK")
        return 0

    print("Paper order candidates: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

"""Run paper-trading risk and compliance gates without staging orders.

Usage:
    python -m scripts.paper_risk_compliance_check --strategy-id v1_base_momentum --portfolio-input portfolio.json

This Step 5 command is a read-only preflight. It reuses the Step 4 candidate
path, validates candidate rows, then evaluates local risk/compliance gates. It
does not connect to IBKR, stage orders, submit orders, cancel orders, reconcile
fills, reset/trip circuit breakers, or require human YES.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.oms.compliance import ComplianceEngine
from execution.oms.order import Order, OrderSide
from scripts.paper_inputs_check import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_STRATEGY_CONFIG,
    CheckRecorder,
    _finite_number,
    load_strategy_config,
    resolve_strategy_id,
)
from scripts.paper_order_candidates_check import (
    DEFAULT_MAX_SNAPSHOT_AGE_DAYS,
    DEFAULT_MIN_DELTA_WEIGHT,
    OrderCandidate,
    PortfolioSnapshot,
    build_order_candidates,
    load_portfolio_snapshot,
)
from scripts.paper_target_check import TargetPortfolio, construct_target_portfolio

DEFAULT_MAX_GROSS_TARGET_WEIGHT = 1.0


@dataclass(frozen=True)
class GateLimits:
    max_position_weight: float
    max_gross_target_weight: float
    allow_shorts: bool
    max_turnover_weight: float | None
    min_order_notional: float


@dataclass(frozen=True)
class GateSummary:
    strategy_id: str
    target_as_of_date: date
    snapshot_as_of: date
    nav: float
    candidate_count: int
    gross_target_weight: float
    max_target_weight: float
    turnover_weight: float


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
        help="Local JSON snapshot with cash and positions; never read from IBKR in Step 5.",
    )
    parser.add_argument("--max-price-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--max-score-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--min-overlap", type=int, default=None)
    parser.add_argument("--min-delta-weight", type=float, default=DEFAULT_MIN_DELTA_WEIGHT)
    parser.add_argument("--max-snapshot-age-days", type=int, default=DEFAULT_MAX_SNAPSHOT_AGE_DAYS)
    parser.add_argument(
        "--max-position-weight",
        type=float,
        default=None,
        help="Single-name target/post-trade weight cap. Defaults to portfolio.max_position_weight.",
    )
    parser.add_argument(
        "--max-gross-target-weight",
        type=float,
        default=DEFAULT_MAX_GROSS_TARGET_WEIGHT,
        help=f"Maximum target gross weight (default: {DEFAULT_MAX_GROSS_TARGET_WEIGHT}).",
    )
    parser.add_argument(
        "--max-turnover-weight",
        type=float,
        default=None,
        help="Optional maximum sum(abs(delta_weight)) allowed for the candidate batch.",
    )
    parser.add_argument(
        "--allow-shorts",
        action="store_true",
        help="Allow target/candidate short exposure. Default is long-only fail-closed.",
    )
    parser.add_argument(
        "--min-order-notional",
        type=float,
        default=0.0,
        help="Optional per-candidate minimum notional for the read-only ComplianceEngine check.",
    )
    return parser.parse_args(argv)


def _positive_fraction(value: Any, label: str) -> float:
    parsed = _finite_number(value)
    if parsed is None or parsed <= 0.0 or parsed > 1.0:
        raise RuntimeError(f"{label} must be finite and in (0, 1]; got {value!r}")
    return parsed


def _positive_finite(value: Any, label: str) -> float:
    parsed = _finite_number(value)
    if parsed is None or parsed <= 0.0:
        raise RuntimeError(f"{label} must be finite and positive; got {value!r}")
    return parsed


def _non_negative_finite(value: Any, label: str) -> float:
    parsed = _finite_number(value)
    if parsed is None or parsed < 0.0:
        raise RuntimeError(f"{label} must be finite and non-negative; got {value!r}")
    return parsed


def _resolve_limits(args: argparse.Namespace, strategy_config: Mapping[str, Any]) -> GateLimits:
    portfolio_cfg = strategy_config.get("portfolio")
    if not isinstance(portfolio_cfg, Mapping):
        raise RuntimeError("Strategy config must include a portfolio mapping")

    raw_max_position = (
        args.max_position_weight
        if args.max_position_weight is not None
        else portfolio_cfg.get("max_position_weight")
    )
    max_position_weight = _positive_fraction(raw_max_position, "max position weight")
    max_gross_target_weight = _positive_finite(args.max_gross_target_weight, "--max-gross-target-weight")
    max_turnover_weight = None
    if args.max_turnover_weight is not None:
        max_turnover_weight = _non_negative_finite(args.max_turnover_weight, "--max-turnover-weight")
    min_order_notional = _non_negative_finite(args.min_order_notional, "--min-order-notional")
    config_allows_shorts = bool(portfolio_cfg.get("allow_shorts", False))
    if "long_only" in portfolio_cfg:
        config_allows_shorts = not bool(portfolio_cfg["long_only"])
    return GateLimits(
        max_position_weight=max_position_weight,
        max_gross_target_weight=max_gross_target_weight,
        allow_shorts=bool(args.allow_shorts) or config_allows_shorts,
        max_turnover_weight=max_turnover_weight,
        min_order_notional=min_order_notional,
    )


def _validate_candidate_schema(candidate: OrderCandidate, idx: int) -> None:
    ticker = str(candidate.ticker).strip().upper()
    if not ticker or ticker != candidate.ticker:
        raise RuntimeError(f"Candidate #{idx} ticker must be a non-empty uppercase symbol")
    if candidate.direction not in {"BUY", "SELL"}:
        raise RuntimeError(f"Candidate {ticker} direction must be BUY or SELL; got {candidate.direction!r}")

    finite_fields = {
        "current_weight": candidate.current_weight,
        "target_weight": candidate.target_weight,
        "delta_weight": candidate.delta_weight,
        "reference_price": candidate.reference_price,
        "estimated_shares": candidate.estimated_shares,
        "estimated_notional": candidate.estimated_notional,
    }
    invalid = [name for name, value in finite_fields.items() if not math.isfinite(float(value))]
    if invalid:
        raise RuntimeError(f"Candidate {ticker} has non-finite fields: {', '.join(invalid)}")
    if candidate.reference_price <= 0.0:
        raise RuntimeError(f"Candidate {ticker} reference_price must be positive")
    if candidate.estimated_shares <= 0.0:
        raise RuntimeError(f"Candidate {ticker} estimated_shares must be positive")
    if candidate.estimated_notional <= 0.0:
        raise RuntimeError(f"Candidate {ticker} estimated_notional must be positive")
    if candidate.direction == "BUY" and candidate.delta_weight <= 0.0:
        raise RuntimeError(f"Candidate {ticker} BUY must have positive delta_weight")
    if candidate.direction == "SELL" and candidate.delta_weight >= 0.0:
        raise RuntimeError(f"Candidate {ticker} SELL must have negative delta_weight")


def _current_quantity_by_ticker(snapshot: PortfolioSnapshot) -> dict[str, float]:
    return {position.ticker: position.quantity for position in snapshot.positions}


def _check_candidates(
    *,
    target: TargetPortfolio,
    snapshot: PortfolioSnapshot,
    candidates: tuple[OrderCandidate, ...],
    limits: GateLimits,
    recorder: CheckRecorder,
) -> GateSummary | None:
    ok = True
    target_weights = {position.ticker.upper(): float(position.target_weight) for position in target.positions}
    current_quantities = _current_quantity_by_ticker(snapshot)

    for idx, candidate in enumerate(candidates, start=1):
        try:
            _validate_candidate_schema(candidate, idx)
        except RuntimeError as exc:
            recorder.fail(str(exc))
            ok = False
            continue

        if not limits.allow_shorts and candidate.target_weight < -1e-9:
            recorder.fail(f"Candidate {candidate.ticker} creates short target weight {candidate.target_weight:.8f}")
            ok = False
        if candidate.direction == "SELL":
            held_quantity = current_quantities.get(candidate.ticker, 0.0)
            if not limits.allow_shorts and candidate.estimated_shares > held_quantity + 1e-6:
                recorder.fail(
                    f"Candidate {candidate.ticker} SELL estimates {candidate.estimated_shares:.6f} "
                    f"shares but local snapshot holds {held_quantity:.6f}"
                )
                ok = False

    for ticker, weight in target_weights.items():
        if not math.isfinite(weight):
            recorder.fail(f"Target weight for {ticker} is not finite")
            ok = False
        if not limits.allow_shorts and weight < -1e-9:
            recorder.fail(f"Target weight for {ticker} is short ({weight:.8f}) but shorts are disabled")
            ok = False
        if abs(weight) > limits.max_position_weight + 1e-9:
            recorder.fail(
                f"Target weight for {ticker} {weight:.6f} exceeds max position "
                f"{limits.max_position_weight:.6f}"
            )
            ok = False

    gross_target_weight = sum(abs(weight) for weight in target_weights.values())
    max_target_weight = max((abs(weight) for weight in target_weights.values()), default=0.0)
    turnover_weight = sum(abs(candidate.delta_weight) for candidate in candidates)
    if not math.isfinite(gross_target_weight):
        recorder.fail("Gross target weight is not finite")
        ok = False
    elif gross_target_weight > limits.max_gross_target_weight + 1e-9:
        recorder.fail(
            f"Gross target weight {gross_target_weight:.6f} exceeds max "
            f"{limits.max_gross_target_weight:.6f}"
        )
        ok = False

    if limits.max_turnover_weight is not None and turnover_weight > limits.max_turnover_weight + 1e-9:
        recorder.fail(
            f"Turnover weight {turnover_weight:.6f} exceeds max {limits.max_turnover_weight:.6f}"
        )
        ok = False

    if ok:
        compliance = ComplianceEngine()
        context = {
            "as_of_date": snapshot.as_of,
            "circuit_breaker_open": False,
            "current_weights": pd.Series(snapshot.current_weights, dtype=float),
            "total_nav": snapshot.nav,
            "max_position_weight": limits.max_position_weight,
            "min_order_notional": limits.min_order_notional,
        }
        for candidate in candidates:
            side = OrderSide.BUY if candidate.direction == "BUY" else OrderSide.SELL
            order = Order(
                ticker=candidate.ticker,
                side=side,
                quantity=candidate.estimated_shares,
                limit_price=candidate.reference_price,
                strategy_id=target.strategy_id,
                notes="paper Step 5 preflight adapter; not staged",
            )
            passed, reason = compliance.check(order, context)
            if not passed:
                recorder.fail(f"ComplianceEngine rejected {candidate.ticker} {candidate.direction}: {reason}")
                ok = False

    if ok:
        recorder.ok(f"{len(candidates)} candidate rows passed schema and finite-value checks")
        recorder.ok(f"Target max position {max_target_weight:.6f} <= {limits.max_position_weight:.6f}")
        recorder.ok(f"Gross target weight {gross_target_weight:.6f} <= {limits.max_gross_target_weight:.6f}")
        if limits.max_turnover_weight is not None:
            recorder.ok(f"Turnover weight {turnover_weight:.6f} <= {limits.max_turnover_weight:.6f}")
        recorder.ok("ComplianceEngine data-only adapter passed all candidates")
        recorder.info(
            "Live circuit-breaker, wash-sale history, and sector maps were not inspected; "
            "circuit_breaker_open is forced false only inside this local preflight"
        )
        return GateSummary(
            strategy_id=target.strategy_id,
            target_as_of_date=target.as_of_date,
            snapshot_as_of=snapshot.as_of,
            nav=snapshot.nav,
            candidate_count=len(candidates),
            gross_target_weight=gross_target_weight,
            max_target_weight=max_target_weight,
            turnover_weight=turnover_weight,
        )
    return None


def _print_summary(summary: GateSummary, limits: GateLimits, recorder: CheckRecorder) -> None:
    recorder.info(
        f"Risk/compliance gates: strategy_id={summary.strategy_id!r}, "
        f"target_as_of_date={summary.target_as_of_date}"
    )
    recorder.info(f"Portfolio snapshot as_of: {summary.snapshot_as_of}")
    recorder.info(f"Current NAV from local snapshot: {summary.nav:.2f}")
    recorder.info(f"Candidate count: {summary.candidate_count}")
    recorder.info(f"Gross target weight: {summary.gross_target_weight:.6f}")
    recorder.info(f"Max target position weight: {summary.max_target_weight:.6f}")
    recorder.info(f"Turnover weight: {summary.turnover_weight:.6f}")
    recorder.info(f"Shorts allowed: {limits.allow_shorts}")
    recorder.info(f"Min order notional: {limits.min_order_notional:.2f}")


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

    recorder.info("Paper risk/compliance check")
    database_url = env_map.get("DATABASE_URL")
    if not database_url:
        recorder.fail("DATABASE_URL must be set to run paper risk/compliance gates")
        print()
        print("Paper risk/compliance: FAILED")
        return 1
    if not args.strategy_id:
        recorder.fail("--strategy-id must be passed explicitly for paper risk/compliance gates")
        print()
        print("Paper risk/compliance: FAILED")
        return 1

    summary: GateSummary | None = None
    limits: GateLimits | None = None
    try:
        strategy_config = load_strategy_config(args.strategy_config)
        strategy_id = resolve_strategy_id(strategy_config, args.strategy_id)
        limits = _resolve_limits(args, strategy_config)
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
            summary = _check_candidates(
                target=target,
                snapshot=snapshot,
                candidates=candidates,
                limits=limits,
                recorder=recorder,
            )
    except Exception as exc:
        recorder.fail(str(exc))

    print()
    if recorder.is_ok and summary is not None and limits is not None:
        _print_summary(summary, limits, recorder)
        print("Paper risk/compliance: OK")
        return 0

    print("Paper risk/compliance: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

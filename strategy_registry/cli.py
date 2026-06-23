"""CLI: python -m strategy_registry <subcommand>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from decimal import Decimal

from strategy_registry.registry import (
    PerformanceSnapshot,
    StrategyRegistry,
    StrategyStatus,
)


def _get_registry() -> StrategyRegistry:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return StrategyRegistry(db_url)


def cmd_register(args: argparse.Namespace) -> None:
    registry = _get_registry()
    strategy = registry.register(
        strategy_id=args.strategy_id,
        config_path=args.config_path,
        notes=args.notes,
    )
    print(
        f"Registered '{strategy.strategy_id}' "
        f"(v{strategy.version}, status={strategy.status}, sha256={strategy.config_sha256[:12]}…)"
    )


def cmd_status(args: argparse.Namespace) -> None:
    registry = _get_registry()
    to_status = StrategyStatus(args.to)
    strategy = registry.transition(
        strategy_id=args.strategy_id,
        to_status=to_status,
        operator_notes=args.notes,
    )
    print(
        f"Transitioned '{strategy.strategy_id}' → status={strategy.status}"
    )


def cmd_list(args: argparse.Namespace) -> None:
    registry = _get_registry()
    status_filter = StrategyStatus(args.status) if args.status else None
    strategies = registry.list(status=status_filter)
    if not strategies:
        print("No strategies found.")
        return
    header = f"{'STRATEGY_ID':<35} {'STATUS':<15} {'VERSION':<8} {'NAME'}"
    print(header)
    print("-" * len(header))
    for s in strategies:
        print(f"{s.strategy_id:<35} {s.status:<15} {s.version:<8} {s.name}")


def cmd_show(args: argparse.Namespace) -> None:
    registry = _get_registry()
    s = registry.get(args.strategy_id)
    data = {
        "strategy_id": s.strategy_id,
        "status": s.status,
        "version": s.version,
        "name": s.name,
        "description": s.description,
        "config_path": s.config_path,
        "config_sha256": s.config_sha256,
        "portfolio_method": s.portfolio_method,
        "n_long": s.n_long,
        "rebalance_frequency": s.rebalance_frequency,
        "registered_at": s.registered_at.isoformat() if s.registered_at else None,
        "activated_paper_at": s.activated_paper_at.isoformat() if s.activated_paper_at else None,
        "activated_live_at": s.activated_live_at.isoformat() if s.activated_live_at else None,
        "archived_at": s.archived_at.isoformat() if s.archived_at else None,
        "notes": s.notes,
    }
    print(json.dumps(data, indent=2))


def cmd_verify(args: argparse.Namespace) -> None:
    registry = _get_registry()
    registry.verify_config_integrity(args.strategy_id)
    print(f"Config integrity verified for '{args.strategy_id}'. No drift detected.")


def cmd_perf(args: argparse.Namespace) -> None:
    registry = _get_registry()
    snapshot = PerformanceSnapshot(
        snapshot_date=date.fromisoformat(args.snapshot_date),
        period_type=args.period_type,
        period_start=date.fromisoformat(args.period_start) if args.period_start else None,
        period_end=date.fromisoformat(args.period_end) if args.period_end else None,
        annualized_return=Decimal(args.annualized_return) if args.annualized_return else None,
        annualized_volatility=Decimal(args.annualized_volatility) if args.annualized_volatility else None,
        sharpe_ratio=Decimal(args.sharpe) if args.sharpe else None,
        max_drawdown=Decimal(args.max_drawdown) if args.max_drawdown else None,
        information_ratio=Decimal(args.information_ratio) if args.information_ratio else None,
        total_trades=int(args.total_trades) if args.total_trades else None,
        data_version=args.data_version,
        mlflow_run_id=args.mlflow_run_id,
    )
    row = registry.record_performance(args.strategy_id, snapshot)
    print(f"Performance snapshot recorded (id={row.id}) for '{args.strategy_id}'.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m strategy_registry",
        description="RQIS Strategy Registry CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # register
    p_reg = sub.add_parser("register", help="Register a new strategy")
    p_reg.add_argument("--strategy-id", required=True)
    p_reg.add_argument("--config-path", required=True)
    p_reg.add_argument("--notes")
    p_reg.set_defaults(func=cmd_register)

    # status (transition)
    p_status = sub.add_parser("status", help="Transition a strategy to a new status")
    p_status.add_argument("--strategy-id", required=True)
    p_status.add_argument(
        "--to",
        required=True,
        choices=[s.value for s in StrategyStatus],
    )
    p_status.add_argument("--notes")
    p_status.set_defaults(func=cmd_status)

    # list
    p_list = sub.add_parser("list", help="List registered strategies")
    p_list.add_argument(
        "--status",
        choices=[s.value for s in StrategyStatus],
        default=None,
    )
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = sub.add_parser("show", help="Show full detail for a strategy")
    p_show.add_argument("--strategy-id", required=True)
    p_show.set_defaults(func=cmd_show)

    # verify (config integrity)
    p_verify = sub.add_parser("verify", help="Verify config file has not drifted (C6)")
    p_verify.add_argument("--strategy-id", required=True)
    p_verify.set_defaults(func=cmd_verify)

    # perf (record performance snapshot)
    p_perf = sub.add_parser("perf", help="Record a performance snapshot")
    p_perf.add_argument("--strategy-id", required=True)
    p_perf.add_argument("--period-type", required=True, choices=["backtest", "paper", "live"])
    p_perf.add_argument("--snapshot-date", required=True, help="YYYY-MM-DD")
    p_perf.add_argument("--period-start", help="YYYY-MM-DD")
    p_perf.add_argument("--period-end", help="YYYY-MM-DD")
    p_perf.add_argument("--annualized-return")
    p_perf.add_argument("--annualized-volatility")
    p_perf.add_argument("--sharpe")
    p_perf.add_argument("--max-drawdown")
    p_perf.add_argument("--information-ratio")
    p_perf.add_argument("--total-trades")
    p_perf.add_argument("--data-version", help="MLflow manifest path (required for backtest, C7)")
    p_perf.add_argument("--mlflow-run-id")
    p_perf.set_defaults(func=cmd_perf)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

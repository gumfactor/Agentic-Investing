"""CLI: python -m strategy_registry <subcommand>"""

from __future__ import annotations

import argparse
import json
import os
import sys

from strategy_registry.registry import (
    StrategyRegistry,
    StrategyStatus,
)


def _registry() -> StrategyRegistry:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return StrategyRegistry(db_url)


# ── subcommand handlers ───────────────────────────────────────────────────────


def cmd_fingerprint(args: argparse.Namespace) -> None:
    """Validate and display config fingerprint without writing to DB."""
    reg = StrategyRegistry.__new__(StrategyRegistry)  # no DB needed
    from strategy_registry import fingerprint as fp_module
    fp = fp_module.fingerprint(args.config_path, args.strategy_id)
    print(json.dumps({
        "strategy_id": fp.strategy_id,
        "config_hash": fp.config_hash,
        "version": fp.version,
        "name": fp.name,
        "description": fp.description,
        "portfolio_method": fp.portfolio_method,
        "n_long": fp.n_long,
        "rebalance_frequency": fp.rebalance_frequency,
        "source_path": fp.source_path,
    }, indent=2))


def cmd_define(args: argparse.Namespace) -> None:
    """Add a config to strategy_definitions (idempotent; pre-registration use)."""
    reg = _registry()
    defn = reg.add_definition(args.config_path, explicit_strategy_id=args.strategy_id)
    print(
        f"Definition recorded: strategy_id={defn.strategy_id!r} "
        f"v{defn.version} config_hash={defn.config_hash[:12]}…"
    )


def cmd_register(args: argparse.Namespace) -> None:
    """Formally register a strategy for operational use."""
    reg = _registry()
    strategy = reg.register(
        config_path=args.config_path,
        strategy_family=args.family,
        supersedes_strategy_id=args.supersedes,
        notes=args.notes,
        explicit_strategy_id=args.strategy_id,
    )
    print(
        f"Registered '{strategy.strategy_id}' "
        f"(v{reg.get_definition(strategy.strategy_id, strategy.canonical_config_hash).version}, "
        f"status={strategy.status}, hash={strategy.canonical_config_hash[:12]}…)"
    )


def cmd_status(args: argparse.Namespace) -> None:
    """Transition a strategy to a new lifecycle status."""
    reg = _registry()
    to_status = StrategyStatus(args.to)
    strategy = reg.transition(
        strategy_id=args.strategy_id,
        to_status=to_status,
        operator_notes=args.notes,
    )
    print(f"'{strategy.strategy_id}' → status={strategy.status}")


def cmd_list(args: argparse.Namespace) -> None:
    """List registered strategies."""
    reg = _registry()
    status_filter = StrategyStatus(args.status) if args.status else None
    strategies = reg.list(status=status_filter, strategy_family=args.family)
    if not strategies:
        print("No strategies found.")
        return
    header = f"{'STRATEGY_ID':<35} {'STATUS':<15} {'FAMILY':<20} {'SUPERSEDES'}"
    print(header)
    print("-" * len(header))
    for s in strategies:
        print(
            f"{s.strategy_id:<35} {s.status:<15} "
            f"{(s.strategy_family or ''):<20} {s.supersedes_strategy_id or ''}"
        )


def cmd_show(args: argparse.Namespace) -> None:
    """Show full detail for a strategy including its canonical definition."""
    reg = _registry()
    s = reg.get(args.strategy_id)
    defn = reg.get_definition(s.strategy_id, s.canonical_config_hash)
    data = {
        "strategy_id": s.strategy_id,
        "status": s.status,
        "strategy_family": s.strategy_family,
        "supersedes_strategy_id": s.supersedes_strategy_id,
        "canonical_config_hash": s.canonical_config_hash,
        "definition": {
            "version": defn.version,
            "name": defn.name,
            "description": defn.description,
            "portfolio_method": defn.portfolio_method,
            "n_long": defn.n_long,
            "rebalance_frequency": defn.rebalance_frequency,
            "source_path": defn.source_path,
            "created_at": defn.created_at.isoformat() if defn.created_at else None,
        },
        "registered_at": s.registered_at.isoformat() if s.registered_at else None,
        "activated_paper_at": s.activated_paper_at.isoformat() if s.activated_paper_at else None,
        "activated_live_at": s.activated_live_at.isoformat() if s.activated_live_at else None,
        "archived_at": s.archived_at.isoformat() if s.archived_at else None,
        "notes": s.notes,
    }
    print(json.dumps(data, indent=2))


def cmd_verify(args: argparse.Namespace) -> None:
    """Re-fingerprint the config on disk and confirm no drift (C6)."""
    reg = _registry()
    reg.verify_config_integrity(args.strategy_id)
    print(f"Config integrity verified for '{args.strategy_id}'. No drift detected.")


def cmd_record_run(args: argparse.Namespace) -> None:
    """Record an experiment run result."""
    reg = _registry()
    metrics: dict = {}
    if args.metrics_json:
        with open(args.metrics_json, encoding="utf-8") as fh:
            metrics = json.load(fh)
        if not isinstance(metrics, dict):
            print("ERROR: --metrics-json must contain a JSON object.", file=sys.stderr)
            sys.exit(1)
    run = reg.record_run(
        strategy_id=args.strategy_id,
        config_hash=args.config_hash,
        run_type=args.run_type,
        status=args.run_status,
        metrics=metrics,
        data_version=args.data_version,
        artifact_path=args.artifact_path,
        mlflow_run_id=args.mlflow_run_id,
        notes=args.notes,
    )
    print(f"Run recorded (id={run.id}) for '{run.strategy_id}' {run.run_type!r} status={run.status!r}")


def cmd_runs(args: argparse.Namespace) -> None:
    """List run records for a strategy."""
    reg = _registry()
    runs = reg.get_runs(
        strategy_id=args.strategy_id,
        run_type=args.run_type,
        status=args.run_status,
    )
    if not runs:
        print("No runs found.")
        return
    header = f"{'ID':<8} {'RUN_TYPE':<14} {'STATUS':<10} {'DATA_VERSION':<50} {'STARTED_AT'}"
    print(header)
    print("-" * len(header))
    for r in runs:
        print(
            f"{r.id:<8} {r.run_type:<14} {r.status:<10} "
            f"{(r.data_version or ''):<50} {r.started_at.isoformat()}"
        )


# ── argument parser ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m strategy_registry",
        description="RQIS Strategy Registry CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fingerprint
    p = sub.add_parser("fingerprint", help="Validate and hash a config (no DB write)")
    p.add_argument("config_path")
    p.add_argument("--strategy-id", default=None)
    p.set_defaults(func=cmd_fingerprint)

    # define
    p = sub.add_parser("define", help="Add a definition to strategy_definitions (idempotent)")
    p.add_argument("config_path")
    p.add_argument("--strategy-id", default=None)
    p.set_defaults(func=cmd_define)

    # register
    p = sub.add_parser("register", help="Formally register a strategy for operational use")
    p.add_argument("config_path")
    p.add_argument("--strategy-id", default=None)
    p.add_argument("--family", default=None, help="Strategy family (e.g. 'base_momentum')")
    p.add_argument("--supersedes", default=None, help="strategy_id this version replaces")
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_register)

    # status
    p = sub.add_parser("status", help="Transition a strategy to a new lifecycle status")
    p.add_argument("--strategy-id", required=True)
    p.add_argument("--to", required=True, choices=[s.value for s in StrategyStatus])
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_status)

    # list
    p = sub.add_parser("list", help="List registered strategies")
    p.add_argument("--status", choices=[s.value for s in StrategyStatus], default=None)
    p.add_argument("--family", default=None)
    p.set_defaults(func=cmd_list)

    # show
    p = sub.add_parser("show", help="Show full detail for a strategy")
    p.add_argument("--strategy-id", required=True)
    p.set_defaults(func=cmd_show)

    # verify
    p = sub.add_parser("verify", help="Confirm config has not drifted from registered hash (C6)")
    p.add_argument("--strategy-id", required=True)
    p.set_defaults(func=cmd_verify)

    # record-run
    p = sub.add_parser("record-run", help="Record an experiment run result")
    p.add_argument("--strategy-id", required=True)
    p.add_argument("--config-hash", required=True)
    p.add_argument(
        "--run-type",
        required=True,
        choices=["unit", "signal_ic", "backtest", "walk_forward", "paper", "live"],
    )
    p.add_argument(
        "--run-status",
        required=True,
        choices=["running", "passed", "failed", "blocked"],
        dest="run_status",
    )
    p.add_argument("--data-version", default=None, help="MLflow manifest path (required for backtest/walk_forward, C7)")
    p.add_argument("--metrics-json", default=None, help="Path to JSON file with metrics dict")
    p.add_argument("--artifact-path", default=None)
    p.add_argument("--mlflow-run-id", default=None)
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_record_run)

    # runs
    p = sub.add_parser("runs", help="List run records for a strategy")
    p.add_argument("--strategy-id", required=True)
    p.add_argument(
        "--run-type",
        choices=["unit", "signal_ic", "backtest", "walk_forward", "paper", "live"],
        default=None,
    )
    p.add_argument(
        "--run-status",
        choices=["running", "passed", "failed", "blocked"],
        default=None,
        dest="run_status",
    )
    p.set_defaults(func=cmd_runs)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

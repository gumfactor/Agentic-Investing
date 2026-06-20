"""Create a local stage-only paper order blotter artifact.

Usage:
    python -m scripts.paper_stage_blotter_check --strategy-id v1_base_momentum --portfolio-input portfolio.json --output blotter.json

This Step 6 command is a local artifact preflight. It reuses the Step 5
risk/compliance path, then writes a JSON blotter for operator review only after
all upstream gates pass. It does not connect to IBKR, instantiate OrderManager,
register OMS orders, submit orders, cancel orders, reconcile fills, or require
human YES.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.paper_inputs_check import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_STRATEGY_CONFIG,
    CheckRecorder,
    load_strategy_config,
    resolve_strategy_id,
)
from scripts.paper_order_candidates_check import (
    DEFAULT_MAX_SNAPSHOT_AGE_DAYS,
    DEFAULT_MIN_DELTA_WEIGHT,
    OrderCandidate,
    build_order_candidates,
    load_portfolio_snapshot,
)
from scripts.paper_risk_compliance_check import (
    DEFAULT_MAX_GROSS_TARGET_WEIGHT,
    GateLimits,
    GateSummary,
    _check_candidates,
    _resolve_limits,
)
from scripts.paper_target_check import construct_target_portfolio

BLOTTER_SCHEMA_VERSION = "paper_stage_blotter.v1"
US_STOCK_MIN_PRICE_INCREMENT = 0.01


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
        help="Local JSON snapshot with cash and positions; never read from IBKR in Step 6.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Explicit local JSON path for the stage-only blotter artifact.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing blotter artifact. Default is fail-closed.",
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
    parser.add_argument(
        "--allow-fractional-shares",
        action="store_true",
        help=(
            "Keep fractional estimated_shares in the local artifact. Default floors to whole "
            "shares because the IBKR TWS API rejects fractional-sized stock orders."
        ),
    )
    return parser.parse_args(argv)


def _json_default(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _candidate_rows(candidates: tuple[OrderCandidate, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sequence, candidate in enumerate(candidates, start=1):
        rows.append(
            {
                "sequence": sequence,
                "ticker": candidate.ticker,
                "direction": candidate.direction,
                "review_status": "LOCAL_STAGE_ONLY",
                "current_weight": candidate.current_weight,
                "target_weight": candidate.target_weight,
                "delta_weight": candidate.delta_weight,
                "reference_price": candidate.reference_price,
                "estimated_shares": candidate.estimated_shares,
                "estimated_notional": candidate.estimated_notional,
            }
        )
    return rows


def _whole_share_candidates(
    candidates: tuple[OrderCandidate, ...],
    *,
    nav: float,
) -> tuple[OrderCandidate, ...]:
    rounded: list[OrderCandidate] = []
    for candidate in candidates:
        whole_shares = math.floor(candidate.estimated_shares)
        if whole_shares <= 0:
            continue
        reference_price = _api_limit_price(candidate.reference_price, candidate.direction)
        notional = whole_shares * reference_price
        weight_delta = notional / nav
        signed_delta = weight_delta if candidate.direction == "BUY" else -weight_delta
        rounded.append(
            OrderCandidate(
                ticker=candidate.ticker,
                direction=candidate.direction,
                current_weight=candidate.current_weight,
                target_weight=candidate.current_weight + signed_delta,
                delta_weight=signed_delta,
                reference_price=reference_price,
                estimated_shares=float(whole_shares),
                estimated_notional=notional,
            )
        )
    return tuple(rounded)


def _api_limit_price(price: float, direction: str) -> float:
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError(f"Reference price must be finite and positive; got {price!r}")
    ticks = price / US_STOCK_MIN_PRICE_INCREMENT
    if direction == "BUY":
        rounded = math.ceil(ticks - 1e-12) * US_STOCK_MIN_PRICE_INCREMENT
    elif direction == "SELL":
        rounded = math.floor(ticks + 1e-12) * US_STOCK_MIN_PRICE_INCREMENT
    else:
        raise RuntimeError(f"Unsupported order direction for limit price rounding: {direction!r}")
    return round(rounded, 2)


def _rounding_summary(
    original: tuple[OrderCandidate, ...],
    rounded: tuple[OrderCandidate, ...],
    *,
    quantity_mode: str,
) -> dict[str, Any]:
    original_notional = sum(candidate.estimated_notional for candidate in original)
    rounded_notional = sum(candidate.estimated_notional for candidate in rounded)
    return {
        "quantity_mode": quantity_mode,
        "original_candidate_count": len(original),
        "rounded_candidate_count": len(rounded),
        "dropped_zero_share_count": len(original) - len(rounded),
        "original_estimated_notional": original_notional,
        "rounded_estimated_notional": rounded_notional,
        "residual_cash_from_rounding": max(0.0, original_notional - rounded_notional),
    }


def _rows_checksum(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _artifact_checksum(artifact: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(artifact))
    payload.pop("artifact_sha256", None)
    return _stable_sha256(payload)


def _build_artifact(
    *,
    strategy_config_path: Path,
    portfolio_input_path: Path,
    run_at: datetime,
    run_id: str,
    target_as_of_date: date,
    snapshot_as_of: date,
    rows: list[dict[str, Any]],
    summary: GateSummary,
    limits: GateLimits,
    output_path: Path,
    rounding: Mapping[str, Any],
) -> dict[str, Any]:
    row_checksum = _rows_checksum(rows)
    provenance = {
        "strategy_config_path": str(strategy_config_path),
        "strategy_config_sha256": _file_sha256(strategy_config_path),
        "portfolio_input_path": str(portfolio_input_path),
        "portfolio_input_sha256": _file_sha256(portfolio_input_path),
        "gate_inputs": {
            "target_as_of_date": target_as_of_date.isoformat(),
            "portfolio_snapshot_as_of": snapshot_as_of.isoformat(),
            "max_position_weight": limits.max_position_weight,
            "max_gross_target_weight": limits.max_gross_target_weight,
            "allow_shorts": limits.allow_shorts,
            "max_turnover_weight": limits.max_turnover_weight,
            "min_order_notional": limits.min_order_notional,
            "quantity_mode": rounding["quantity_mode"],
        },
    }
    provenance["gate_inputs_sha256"] = _stable_sha256(provenance["gate_inputs"])
    return {
        "schema_version": BLOTTER_SCHEMA_VERSION,
        "artifact_type": "paper_stage_only_order_blotter",
        "run_id": run_id,
        "generated_at_utc": run_at.astimezone(UTC).isoformat(),
        "paper_only": True,
        "stage_only": True,
        "strategy_id": summary.strategy_id,
        "strategy_config": str(strategy_config_path),
        "provenance": provenance,
        "source": {
            "step5_required": True,
            "target_as_of_date": target_as_of_date.isoformat(),
            "portfolio_snapshot_as_of": snapshot_as_of.isoformat(),
        },
        "safety": {
            "broker_connected": False,
            "broker_order_ids_present": False,
            "order_manager_registered": False,
            "orders_submitted": False,
            "orders_cancelled": False,
            "fills_reconciled": False,
            "human_yes_consumed": False,
        },
        "risk_compliance_summary": {
            "nav": summary.nav,
            "candidate_count": summary.candidate_count,
            "gross_target_weight": summary.gross_target_weight,
            "max_target_weight": summary.max_target_weight,
            "turnover_weight": summary.turnover_weight,
            "max_position_weight": limits.max_position_weight,
            "max_gross_target_weight": limits.max_gross_target_weight,
            "allow_shorts": limits.allow_shorts,
            "max_turnover_weight": limits.max_turnover_weight,
            "min_order_notional": limits.min_order_notional,
        },
        "rounding_summary": dict(rounding),
        "candidate_rows_sha256": row_checksum,
        "candidate_rows": rows,
        "next_step_hint": "Operator review artifact only; Step 7 must revalidate before any OMS registration.",
        "output_path": str(output_path),
    }


def _write_artifact(path: Path, artifact: Mapping[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise RuntimeError(f"Output artifact already exists: {path}. Pass --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True, default=_json_default) + "\n",
            encoding="utf-8",
        )
        if overwrite:
            temp_path.replace(path)
        else:
            try:
                os.link(temp_path, path)
            except FileExistsError as exc:
                raise RuntimeError(
                    f"Output artifact already exists: {path}. Pass --overwrite to replace it."
                ) from exc
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _print_summary(path: Path, artifact: Mapping[str, Any], recorder: CheckRecorder) -> None:
    recorder.info(f"Stage-only blotter artifact: {path}")
    recorder.info(f"Run id: {artifact['run_id']}")
    recorder.info(f"Candidate rows: {len(artifact['candidate_rows'])}")
    recorder.info(f"Candidate row checksum: {artifact['candidate_rows_sha256']}")
    recorder.info("Artifact is local paper-only review data; no broker or OMS staging boundary was crossed")


def run(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    engine_factory: Callable[[str], Engine] = create_engine,
    today_fn: Callable[[], date] = date.today,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> int:
    args = parse_args(argv)
    load_dotenv()
    env_map = os.environ if env is None else env
    recorder = CheckRecorder()

    recorder.info("Paper stage-only blotter check")
    database_url = env_map.get("DATABASE_URL")
    if not database_url:
        recorder.fail("DATABASE_URL must be set to stage a local paper blotter artifact")
        print()
        print("Paper stage-only blotter: FAILED")
        return 1
    if not args.strategy_id:
        recorder.fail("--strategy-id must be passed explicitly for paper blotter staging")
        print()
        print("Paper stage-only blotter: FAILED")
        return 1
    if env_map.get("PAPER_RUN_CLEARED", "false").strip().lower() == "true":
        recorder.fail("PAPER_RUN_CLEARED=true is a live-trading clearance flag; unset it for stage-only paper blotters")
        print()
        print("Paper stage-only blotter: FAILED")
        return 1
    if args.output.exists() and not args.overwrite:
        recorder.fail(f"Output artifact already exists: {args.output}. Pass --overwrite to replace it.")
        print()
        print("Paper stage-only blotter: FAILED")
        return 1

    artifact: dict[str, Any] | None = None
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
            raw_candidates = build_order_candidates(
                target=target,
                snapshot=snapshot,
                min_delta_weight=args.min_delta_weight,
            )
            quantity_mode = "fractional" if args.allow_fractional_shares else "whole_shares"
            candidates = (
                raw_candidates
                if args.allow_fractional_shares
                else _whole_share_candidates(raw_candidates, nav=snapshot.nav)
            )
            rounding = _rounding_summary(
                raw_candidates,
                candidates,
                quantity_mode=quantity_mode,
            )
            if raw_candidates and not candidates:
                raise RuntimeError(
                    "Whole-share rounding dropped all order candidates; "
                    "increase account size, lower prices, or run only a diagnostic fractional blotter."
                )
            summary = _check_candidates(
                target=target,
                snapshot=snapshot,
                candidates=candidates,
                limits=limits,
                recorder=recorder,
            )
            if summary is not None:
                rows = _candidate_rows(candidates)
                artifact = _build_artifact(
                    strategy_config_path=args.strategy_config,
                    portfolio_input_path=args.portfolio_input,
                    run_at=now_fn(),
                    run_id=run_id_factory(),
                    target_as_of_date=target.as_of_date,
                    snapshot_as_of=snapshot.as_of,
                    rows=rows,
                    summary=summary,
                    limits=limits,
                    output_path=args.output,
                    rounding=rounding,
                )
                artifact["artifact_sha256"] = _artifact_checksum(artifact)
                _write_artifact(args.output, artifact, overwrite=args.overwrite)
    except Exception as exc:
        recorder.fail(str(exc))

    print()
    if recorder.is_ok and artifact is not None:
        _print_summary(args.output, artifact, recorder)
        print("Paper stage-only blotter: OK")
        return 0

    print("Paper stage-only blotter: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

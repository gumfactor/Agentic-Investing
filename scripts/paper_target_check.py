"""Construct paper-trading target weights without generating orders.

Usage:
    python -m scripts.paper_target_check --strategy-id v1_base_momentum

This Step 3 command is read-only. It loads the latest strategy inputs from the
database and builds the target portfolio for the configured strategy method. It
does not read broker positions, generate order candidates, stage orders, submit
orders, cancel orders, or reconcile fills.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
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
    _as_date,
    _finite_number,
    _load_latest_prices,
    _load_latest_scores,
    load_strategy_config,
    portfolio_n_long,
    resolve_strategy_id,
    validate_inputs,
)


@dataclass(frozen=True)
class TargetPosition:
    ticker: str
    target_weight: float
    latest_close: float
    alpha_score: float


@dataclass(frozen=True)
class TargetPortfolio:
    strategy_id: str
    method: str
    as_of_date: date
    positions: tuple[TargetPosition, ...]
    cash_weight: float

    @property
    def gross_weight(self) -> float:
        return sum(pos.target_weight for pos in self.positions)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategy-config",
        type=Path,
        default=DEFAULT_STRATEGY_CONFIG,
        help=f"Strategy YAML path (default: {DEFAULT_STRATEGY_CONFIG}).",
    )
    parser.add_argument("--strategy-id", default=None, help="Database strategy_id to load from alpha_scores.")
    parser.add_argument("--max-price-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--max-score-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--min-overlap", type=int, default=None)
    return parser.parse_args(argv)


def construct_equal_weight_targets(
    *,
    strategy_id: str,
    config: Mapping[str, Any],
    price_rows: list[Mapping[str, Any]],
    score_rows: list[Mapping[str, Any]],
    as_of_date: date,
) -> TargetPortfolio:
    n_long = portfolio_n_long(config)
    portfolio_cfg = config.get("portfolio")
    if not isinstance(portfolio_cfg, Mapping):
        raise RuntimeError("Strategy config must include a portfolio mapping")

    max_position_weight = _finite_number(portfolio_cfg.get("max_position_weight", 1.0))
    if max_position_weight is None or max_position_weight <= 0 or max_position_weight > 1:
        raise RuntimeError(
            f"portfolio.max_position_weight must be in (0, 1]; got {portfolio_cfg.get('max_position_weight')!r}"
        )

    price_by_ticker: dict[str, float] = {}
    for row in price_rows:
        ticker = str(row["ticker"])
        close = _finite_number(row["close"])
        if close is not None and close > 0:
            price_by_ticker[ticker] = close

    score_values: list[tuple[str, float, float]] = []
    for row in score_rows:
        ticker = str(row["ticker"])
        score = _finite_number(row["alpha_score"])
        if score is not None:
            rank = _finite_number(row.get("rank"))
            rank_key = rank if rank is not None and rank > 0 else float("inf")
            score_values.append((ticker, score, rank_key))

    top = sorted(score_values, key=lambda item: (-item[1], item[2], item[0]))[:n_long]
    if len(top) < n_long:
        raise RuntimeError(f"Only {len(top)} valid scores available; need {n_long}")

    missing_prices = [ticker for ticker, _score, _rank in top if ticker not in price_by_ticker]
    if missing_prices:
        raise RuntimeError(f"Top scored tickers missing latest prices: {', '.join(missing_prices[:10])}")

    weight = min(1.0 / len(top), max_position_weight)
    positions = tuple(
        TargetPosition(
            ticker=ticker,
            target_weight=weight,
            latest_close=price_by_ticker[ticker],
            alpha_score=score,
        )
        for ticker, score, _rank in top
    )
    gross_weight = sum(pos.target_weight for pos in positions)
    cash_weight = max(0.0, 1.0 - gross_weight)
    return TargetPortfolio(
        strategy_id=strategy_id,
        method="equal_weight",
        as_of_date=as_of_date,
        positions=positions,
        cash_weight=cash_weight,
    )


def construct_target_portfolio(
    *,
    engine: Engine,
    strategy_config_path: Path,
    strategy_config: Mapping[str, Any],
    strategy_id: str,
    max_price_age_days: int,
    max_score_age_days: int,
    min_overlap: int | None,
    today: date,
    recorder: CheckRecorder,
) -> TargetPortfolio | None:
    summary = validate_inputs(
        engine=engine,
        strategy_config_path=strategy_config_path,
        strategy_config=strategy_config,
        strategy_id=strategy_id,
        max_price_age_days=max_price_age_days,
        max_score_age_days=max_score_age_days,
        min_overlap=min_overlap,
        today=today,
        recorder=recorder,
    )
    if summary is None:
        return None

    portfolio_cfg = strategy_config.get("portfolio")
    if not isinstance(portfolio_cfg, Mapping):
        recorder.fail("Strategy config must include a portfolio mapping")
        return None
    method = str(portfolio_cfg.get("method", "equal_weight"))
    if method != "equal_weight":
        recorder.fail(
            f"Portfolio method {method!r} is not supported by Step 3 paper target construction yet"
        )
        return None

    raw_price_date, price_rows = _load_latest_prices(engine)
    raw_score_date, score_rows = _load_latest_scores(engine, strategy_id)
    if raw_price_date is None or raw_score_date is None:
        recorder.fail("Latest prices or scores disappeared during target construction")
        return None
    price_date = _as_date(raw_price_date, "daily_prices latest date")
    score_date = _as_date(raw_score_date, "alpha_scores latest score_date")
    if score_date > price_date:
        recorder.fail(
            f"alpha_scores date {score_date} is newer than latest daily_prices date {price_date}"
        )
        return None

    try:
        return construct_equal_weight_targets(
            strategy_id=strategy_id,
            config=strategy_config,
            price_rows=price_rows,
            score_rows=score_rows,
            as_of_date=summary.score_date,
        )
    except RuntimeError as exc:
        recorder.fail(str(exc))
        return None


def _print_target(target: TargetPortfolio, recorder: CheckRecorder) -> None:
    recorder.info(
        f"Target portfolio: strategy_id={target.strategy_id!r}, method={target.method!r}, "
        f"as_of_date={target.as_of_date}"
    )
    recorder.info(f"Gross target weight: {target.gross_weight:.6f}")
    recorder.info(f"Cash residual weight: {target.cash_weight:.6f}")
    print("ticker,target_weight,latest_close,alpha_score")
    for pos in target.positions:
        print(f"{pos.ticker},{pos.target_weight:.8f},{pos.latest_close:.6f},{pos.alpha_score:.8f}")


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

    recorder.info("Paper target portfolio check")
    database_url = env_map.get("DATABASE_URL")
    if not database_url:
        recorder.fail("DATABASE_URL must be set to construct paper target weights")
        print()
        print("Paper target: FAILED")
        return 1
    if not args.strategy_id:
        recorder.fail("--strategy-id must be passed explicitly for paper target construction")
        print()
        print("Paper target: FAILED")
        return 1

    try:
        strategy_config = load_strategy_config(args.strategy_config)
        strategy_id = resolve_strategy_id(strategy_config, args.strategy_id)
        engine = engine_factory(database_url)
        target = construct_target_portfolio(
            engine=engine,
            strategy_config_path=args.strategy_config,
            strategy_config=strategy_config,
            strategy_id=strategy_id,
            max_price_age_days=args.max_price_age_days,
            max_score_age_days=args.max_score_age_days,
            min_overlap=args.min_overlap,
            today=today_fn(),
            recorder=recorder,
        )
    except Exception as exc:
        recorder.fail(str(exc))
        target = None

    print()
    if recorder.is_ok and target is not None:
        _print_target(target, recorder)
        print("Paper target: OK")
        return 0

    print("Paper target: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

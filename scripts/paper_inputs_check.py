"""Validate paper-trading strategy inputs without constructing orders.

Usage:
    python -m scripts.paper_inputs_check --strategy-id v1

This Step 2 preflight is read-only. It verifies that the configured strategy
has recent market prices and alpha scores in the database, and that enough
scored tickers also have prices for the next portfolio-construction slice.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).parent.parent))


DEFAULT_STRATEGY_CONFIG = Path("config/strategy/v1_base_momentum.yaml")
DEFAULT_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class StrategyInputSummary:
    strategy_config: Path
    strategy_name: str
    strategy_version: str
    strategy_id: str
    price_date: date
    price_rows: int
    score_date: date
    score_rows: int
    required_overlap: int
    overlap_rows: int
    top_scores: tuple[str, ...]


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
    parser.add_argument(
        "--strategy-config",
        type=Path,
        default=DEFAULT_STRATEGY_CONFIG,
        help=f"Strategy YAML path (default: {DEFAULT_STRATEGY_CONFIG}).",
    )
    parser.add_argument(
        "--strategy-id",
        default=None,
        help="Database strategy_id to load from alpha_scores. Required for paper input checks.",
    )
    parser.add_argument(
        "--max-price-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help="Maximum calendar age for the latest daily_prices date.",
    )
    parser.add_argument(
        "--max-score-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help="Maximum calendar age for the latest alpha_scores score_date.",
    )
    parser.add_argument(
        "--min-overlap",
        type=int,
        default=None,
        help="Minimum scored tickers that must also have latest prices. Defaults to portfolio.n_long.",
    )
    return parser.parse_args(argv)


def load_strategy_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Strategy config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise RuntimeError(f"Strategy config must be a mapping: {path}")
    return raw


def resolve_strategy_id(config: Mapping[str, Any], explicit_strategy_id: str | None) -> str:
    if explicit_strategy_id:
        return explicit_strategy_id
    configured = config.get("strategy_id")
    if configured:
        return str(configured)
    version = config.get("version")
    if version is not None:
        return f"v{version}"
    name = config.get("name")
    if name:
        return str(name)
    raise RuntimeError("Strategy id is required: pass --strategy-id or set strategy_id/version/name in config")


def portfolio_n_long(config: Mapping[str, Any]) -> int:
    portfolio = config.get("portfolio")
    if not isinstance(portfolio, Mapping):
        raise RuntimeError("Strategy config must include a portfolio mapping")
    raw = portfolio.get("n_long")
    if raw is None:
        raise RuntimeError("Strategy config portfolio.n_long is required")
    try:
        n_long = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"portfolio.n_long must be an integer; got {raw!r}") from exc
    if n_long <= 0:
        raise RuntimeError(f"portfolio.n_long must be positive; got {n_long}")
    return n_long


def _as_date(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise RuntimeError(f"{label} must be a valid date; got {value!r}") from exc


def _check_age(label: str, value: date, max_age_days: int, today: date, recorder: CheckRecorder) -> bool:
    if max_age_days < 0:
        recorder.fail(f"{label} max age must be non-negative; got {max_age_days}")
        return False
    age_days = (today - value).days
    if age_days < 0:
        recorder.fail(f"{label} date {value} is in the future relative to {today}")
        return False
    if age_days > max_age_days:
        recorder.fail(f"{label} date {value} is stale: {age_days} days old, max {max_age_days}")
        return False
    recorder.ok(f"{label} date {value} is recent ({age_days} days old)")
    return True


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _load_latest_prices(engine: Engine) -> tuple[Any, list[Mapping[str, Any]]]:
    with engine.connect() as conn:
        latest = conn.execute(text("SELECT MAX(date) FROM daily_prices")).scalar()
        if latest is None:
            return None, []
        rows = conn.execute(
            text(
                """
                SELECT ticker, date, close
                FROM daily_prices
                WHERE date = :price_date
                ORDER BY ticker
                """
            ),
            {"price_date": latest},
        ).mappings().all()
    return latest, rows


_OPERATIONAL_METHODOLOGY_NAME = "daily_signal_pipeline_operational"


def _resolve_active_research_run_id(engine: Engine) -> int:
    """The explicitly active research run for the operational methodology
    (BUG-009 section 4 / migration 012). research_run_id is now part of
    alpha_scores'/factor_scores' identity: multiple rows can legitimately
    coexist for the same (ticker, score_date, strategy_id) across runs
    (legacy, superseded, active). Paper-trading input validation must read
    only the active run's rows -- never all of them -- or a backfill/run
    rotation can silently double the score set (or feed a stale/wrong-
    methodology score into order construction). Fails closed: by the time
    alpha_scores has any rows written by daily_signal_pipeline.py, an active
    run for this methodology must already exist (the DAG's own write path
    requires one -- see scripts/register_operational_research_run.py).
    """
    from sqlalchemy.orm import Session

    from data.research.identity import get_active_research_run

    with Session(engine) as session:
        try:
            run = get_active_research_run(session, _OPERATIONAL_METHODOLOGY_NAME)
        except Exception as exc:
            # Broad on purpose: a missing research_methodologies/research_runs
            # table (e.g. migration 012 not yet applied) raises a raw
            # SQLAlchemy error, not ResearchIdentityError; either way, the
            # actionable remediation is the same, and this path must fail
            # closed rather than silently reading unfiltered alpha_scores.
            raise RuntimeError(
                f"No active research run for methodology "
                f"{_OPERATIONAL_METHODOLOGY_NAME!r} (BUG-009 section 4 / migration "
                "012). Run 'python -m scripts.register_operational_research_run' "
                "once (see docs/runbooks/research_run_registration.md), and confirm "
                "daily_signal_pipeline has actually written scores, before running "
                f"paper-trading input checks. Underlying error: {exc}"
            ) from exc
        return run.id


def _load_latest_scores(engine: Engine, strategy_id: str) -> tuple[Any, list[Mapping[str, Any]]]:
    research_run_id = _resolve_active_research_run_id(engine)
    with engine.connect() as conn:
        latest = conn.execute(
            text(
                "SELECT MAX(score_date) FROM alpha_scores "
                "WHERE strategy_id = :strategy_id AND research_run_id = :run_id"
            ),
            {"strategy_id": strategy_id, "run_id": research_run_id},
        ).scalar()
        if latest is None:
            return None, []
        rows = conn.execute(
            text(
                """
                SELECT ticker, score_date, strategy_id, alpha_score, rank, universe_size
                FROM alpha_scores
                WHERE strategy_id = :strategy_id
                  AND score_date = :score_date
                  AND research_run_id = :run_id
                ORDER BY ticker
                """
            ),
            {"strategy_id": strategy_id, "score_date": latest, "run_id": research_run_id},
        ).mappings().all()
    return latest, rows


def validate_inputs(
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
) -> StrategyInputSummary | None:
    required_overlap = min_overlap if min_overlap is not None else portfolio_n_long(strategy_config)
    if required_overlap <= 0:
        recorder.fail(f"Minimum overlap must be positive; got {required_overlap}")
        return None

    raw_price_date, price_rows = _load_latest_prices(engine)
    if raw_price_date is None or not price_rows:
        recorder.fail("daily_prices is empty")
        return None
    price_date = _as_date(raw_price_date, "daily_prices latest date")

    raw_score_date, score_rows = _load_latest_scores(engine, strategy_id)
    if raw_score_date is None or not score_rows:
        recorder.fail(f"No alpha_scores found for strategy_id={strategy_id!r}")
        return None
    score_date = _as_date(raw_score_date, "alpha_scores latest score_date")

    ok = True
    ok = _check_age("daily_prices", price_date, max_price_age_days, today, recorder) and ok
    ok = _check_age("alpha_scores", score_date, max_score_age_days, today, recorder) and ok

    price_tickers: set[str] = set()
    for row in price_rows:
        ticker = str(row["ticker"])
        close = _finite_number(row["close"])
        if close is None or close <= 0:
            recorder.fail(f"daily_prices has invalid close for {ticker} on {price_date}: {row['close']!r}")
            ok = False
        price_tickers.add(ticker)

    score_values: list[tuple[str, float]] = []
    score_tickers: set[str] = set()
    for row in score_rows:
        ticker = str(row["ticker"])
        score = _finite_number(row["alpha_score"])
        if score is None:
            recorder.fail(f"alpha_scores has invalid alpha_score for {ticker} on {score_date}: {row['alpha_score']!r}")
            ok = False
        else:
            score_values.append((ticker, score))
        score_tickers.add(ticker)

    if len(score_rows) < required_overlap:
        recorder.fail(
            f"alpha_scores has {len(score_rows)} rows for strategy_id={strategy_id!r}; "
            f"need at least {required_overlap}"
        )
        ok = False

    overlap = price_tickers & score_tickers
    if len(overlap) < required_overlap:
        recorder.fail(
            f"Only {len(overlap)} scored tickers have latest prices; need at least {required_overlap}"
        )
        ok = False
    else:
        recorder.ok(f"{len(overlap)} scored tickers have latest prices (minimum {required_overlap})")

    sorted_scores = sorted(score_values, key=lambda item: item[1], reverse=True)
    top_required = tuple(ticker for ticker, _ in sorted_scores[:required_overlap])
    missing_top_prices = [ticker for ticker in top_required if ticker not in price_tickers]
    if missing_top_prices:
        preview = ", ".join(missing_top_prices[:10])
        suffix = "" if len(missing_top_prices) <= 10 else f", ... ({len(missing_top_prices)} total)"
        recorder.fail(
            f"Top {required_overlap} scored tickers missing latest prices: {preview}{suffix}"
        )
        ok = False

    if not ok:
        return None

    top_scores = tuple(ticker for ticker, _ in sorted_scores[:5])
    return StrategyInputSummary(
        strategy_config=strategy_config_path,
        strategy_name=str(strategy_config.get("name", "")),
        strategy_version=str(strategy_config.get("version", "")),
        strategy_id=strategy_id,
        price_date=price_date,
        price_rows=len(price_rows),
        score_date=score_date,
        score_rows=len(score_rows),
        required_overlap=required_overlap,
        overlap_rows=len(overlap),
        top_scores=top_scores,
    )


def _print_summary(summary: StrategyInputSummary, recorder: CheckRecorder) -> None:
    recorder.info(f"Strategy config: {summary.strategy_config}")
    recorder.info(
        f"Strategy: name={summary.strategy_name!r}, version={summary.strategy_version!r}, "
        f"strategy_id={summary.strategy_id!r}"
    )
    recorder.info(f"Latest prices: {summary.price_rows} rows on {summary.price_date}")
    recorder.info(f"Latest alpha scores: {summary.score_rows} rows on {summary.score_date}")
    recorder.info(
        f"Overlap: {summary.overlap_rows} scored tickers with latest prices "
        f"(minimum {summary.required_overlap})"
    )
    recorder.info(f"Top scored tickers: {', '.join(summary.top_scores)}")


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

    recorder.info("Paper strategy input check")
    database_url = env_map.get("DATABASE_URL")
    if not database_url:
        recorder.fail("DATABASE_URL must be set to load paper-trading inputs")
        print()
        print("Paper inputs: FAILED")
        return 1
    if not args.strategy_id:
        recorder.fail("--strategy-id must be passed explicitly for paper input checks")
        print()
        print("Paper inputs: FAILED")
        return 1

    try:
        strategy_config = load_strategy_config(args.strategy_config)
        strategy_id = resolve_strategy_id(strategy_config, args.strategy_id)
        engine = engine_factory(database_url)
        summary = validate_inputs(
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
        summary = None

    print()
    if recorder.is_ok and summary is not None:
        _print_summary(summary, recorder)
        print("Paper inputs: OK")
        return 0

    print("Paper inputs: FAILED")
    for issue in recorder.issues:
        print(f"- {issue}")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

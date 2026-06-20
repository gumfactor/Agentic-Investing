"""Tests for the paper risk/compliance preflight command."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from scripts import paper_risk_compliance_check as check
from scripts.paper_order_candidates_check import CurrentPosition, OrderCandidate, PortfolioSnapshot
from scripts.paper_target_check import TargetPortfolio, TargetPosition


def _write_config(
    path: Path,
    *,
    method: str = "equal_weight",
    n_long: int = 2,
    max_position_weight: float = 0.60,
    allow_shorts: bool | None = None,
) -> None:
    lines = [
        "version: 1",
        "name: base_momentum",
        "portfolio:",
        f"  method: {method}",
        f"  n_long: {n_long}",
        f"  max_position_weight: {max_position_weight}",
    ]
    if allow_shorts is not None:
        lines.append(f"  allow_shorts: {str(allow_shorts).lower()}")
    lines.append("")
    path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def _write_portfolio(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _engine(
    *,
    price_date: str = "2026-06-19",
    score_date: str = "2026-06-19",
    strategy_id: str = "v1_base_momentum",
):
    engine = create_engine("sqlite://")
    prices = [
        {"ticker": "AAPL", "date": price_date, "close": 200.0},
        {"ticker": "MSFT", "date": price_date, "close": 450.0},
        {"ticker": "NVDA", "date": price_date, "close": 130.0},
    ]
    scores = [
        {
            "ticker": "AAPL",
            "score_date": score_date,
            "strategy_id": strategy_id,
            "alpha_score": 0.8,
            "rank": 2,
            "universe_size": 3,
        },
        {
            "ticker": "MSFT",
            "score_date": score_date,
            "strategy_id": strategy_id,
            "alpha_score": 1.2,
            "rank": 1,
            "universe_size": 3,
        },
        {
            "ticker": "NVDA",
            "score_date": score_date,
            "strategy_id": strategy_id,
            "alpha_score": 0.4,
            "rank": 3,
            "universe_size": 3,
        },
    ]
    pd.DataFrame(prices).to_sql("daily_prices", engine, index=False)
    pd.DataFrame(scores).to_sql("alpha_scores", engine, index=False)
    return engine


def _env() -> dict[str, str]:
    return {"DATABASE_URL": "sqlite://"}


def _pass_args(config_path: Path, portfolio_path: Path) -> list[str]:
    return [
        "--strategy-config",
        str(config_path),
        "--strategy-id",
        "v1_base_momentum",
        "--portfolio-input",
        str(portfolio_path),
    ]


def test_run_passes_read_only_risk_and_compliance_gates(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(
        portfolio_path,
        {
            "as_of": "2026-06-20",
            "cash": 100.0,
            "positions": [
                {"ticker": "NVDA", "quantity": 10.0, "price": 130.0},
                {"ticker": "AAPL", "quantity": 2.0, "price": 200.0},
            ],
        },
    )
    engine = _engine()

    result = check.run(
        _pass_args(config_path, portfolio_path),
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper risk/compliance: OK" in out
    assert "ComplianceEngine data-only adapter passed all candidates" in out
    assert "Live circuit-breaker, wash-sale history, and sector maps were not inspected" in out
    assert "Gross target weight: 1.000000" in out


def test_step_five_does_not_import_broker_or_order_manager():
    source = Path(check.__file__).read_text(encoding="utf-8")

    assert "execution.brokers" not in source
    assert "order_manager" not in source
    assert "OrderManager" not in source


def test_run_requires_explicit_strategy_id(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 100.0, "positions": []})

    result = check.run(
        ["--strategy-config", str(config_path), "--portfolio-input", str(portfolio_path)],
        env=_env(),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "--strategy-id must be passed explicitly" in out


def test_run_fails_when_step_three_target_gate_fails(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 100.0, "positions": []})
    engine = _engine(score_date="2026-06-01")

    result = check.run(
        _pass_args(config_path, portfolio_path),
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "alpha_scores date 2026-06-01 is stale" in out


def test_run_fails_gross_target_limit(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 1000.0, "positions": []})
    engine = _engine()

    result = check.run(
        [*_pass_args(config_path, portfolio_path), "--max-gross-target-weight", "0.90"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Gross target weight 1.000000 exceeds max 0.900000" in out


def test_run_accepts_explicit_gross_limit_above_one(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 1000.0, "positions": []})
    engine = _engine()

    result = check.run(
        [*_pass_args(config_path, portfolio_path), "--max-gross-target-weight", "1.20"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper risk/compliance: OK" in out


def test_run_fails_turnover_limit(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 1000.0, "positions": []})
    engine = _engine()

    result = check.run(
        [*_pass_args(config_path, portfolio_path), "--max-turnover-weight", "0.50"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Turnover weight 1.000000 exceeds max 0.500000" in out


def test_run_fails_min_order_notional(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 1000.0, "positions": []})
    engine = _engine()

    result = check.run(
        [*_pass_args(config_path, portfolio_path), "--min-order-notional", "1000"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "below_min_notional" in out


def test_run_fails_position_limit_from_override(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 1000.0, "positions": []})
    engine = _engine()

    result = check.run(
        [*_pass_args(config_path, portfolio_path), "--max-position-weight", "0.40"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Target weight for" in out
    assert "exceeds max position 0.400000" in out


def test_check_candidates_rejects_invalid_candidate_schema():
    recorder = check.CheckRecorder()
    target = TargetPortfolio(
        strategy_id="v1_base_momentum",
        method="equal_weight",
        as_of_date=date(2026, 6, 20),
        positions=(TargetPosition("AAPL", 0.5, 200.0, 1.0),),
        cash_weight=0.5,
    )
    snapshot = PortfolioSnapshot(
        as_of=date(2026, 6, 20),
        cash=1000.0,
        positions=(),
    )
    limits = check.GateLimits(
        max_position_weight=0.60,
        max_gross_target_weight=1.0,
        allow_shorts=False,
        max_turnover_weight=None,
        min_order_notional=0.0,
    )

    summary = check._check_candidates(
        target=target,
        snapshot=snapshot,
        candidates=(
            OrderCandidate(
                ticker="AAPL",
                direction="BUY",
                current_weight=0.0,
                target_weight=0.5,
                delta_weight=0.5,
                reference_price=0.0,
                estimated_shares=2.5,
                estimated_notional=500.0,
            ),
        ),
        limits=limits,
        recorder=recorder,
    )

    assert summary is None
    assert any("reference_price must be positive" in issue for issue in recorder.issues)


def test_check_candidates_rejects_short_target_when_disabled():
    recorder = check.CheckRecorder()
    target = TargetPortfolio(
        strategy_id="v1_base_momentum",
        method="equal_weight",
        as_of_date=date(2026, 6, 20),
        positions=(TargetPosition("AAPL", -0.1, 200.0, 1.0),),
        cash_weight=1.1,
    )
    snapshot = PortfolioSnapshot(as_of=date(2026, 6, 20), cash=1000.0, positions=())
    limits = check.GateLimits(
        max_position_weight=0.60,
        max_gross_target_weight=1.0,
        allow_shorts=False,
        max_turnover_weight=None,
        min_order_notional=0.0,
    )

    summary = check._check_candidates(
        target=target,
        snapshot=snapshot,
        candidates=(),
        limits=limits,
        recorder=recorder,
    )

    assert summary is None
    assert any("short" in issue for issue in recorder.issues)


def test_resolve_limits_allows_shorts_from_strategy_config(tmp_path):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, allow_shorts=True)
    strategy_config = check.load_strategy_config(config_path)
    args = check.parse_args(
        [
            "--strategy-config",
            str(config_path),
            "--strategy-id",
            "v1_base_momentum",
            "--portfolio-input",
            str(tmp_path / "portfolio.json"),
        ]
    )

    limits = check._resolve_limits(args, strategy_config)

    assert limits.allow_shorts is True


def test_resolve_limits_allows_shorts_from_long_only_false(tmp_path):
    config_path = tmp_path / "strategy.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "name: base_momentum",
                "portfolio:",
                "  method: equal_weight",
                "  n_long: 2",
                "  max_position_weight: 0.60",
                "  long_only: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    strategy_config = check.load_strategy_config(config_path)
    args = check.parse_args(
        [
            "--strategy-config",
            str(config_path),
            "--strategy-id",
            "v1_base_momentum",
            "--portfolio-input",
            str(tmp_path / "portfolio.json"),
        ]
    )

    limits = check._resolve_limits(args, strategy_config)

    assert limits.allow_shorts is True


def test_check_candidates_rejects_sell_beyond_local_holding():
    recorder = check.CheckRecorder()
    target = TargetPortfolio(
        strategy_id="v1_base_momentum",
        method="equal_weight",
        as_of_date=date(2026, 6, 20),
        positions=(TargetPosition("AAPL", 0.0, 200.0, 1.0),),
        cash_weight=1.0,
    )
    snapshot = PortfolioSnapshot(
        as_of=date(2026, 6, 20),
        cash=100.0,
        positions=(CurrentPosition(ticker="AAPL", quantity=1.0, price=200.0),),
    )
    limits = check.GateLimits(
        max_position_weight=0.60,
        max_gross_target_weight=1.0,
        allow_shorts=False,
        max_turnover_weight=None,
        min_order_notional=0.0,
    )

    summary = check._check_candidates(
        target=target,
        snapshot=snapshot,
        candidates=(
            OrderCandidate(
                ticker="AAPL",
                direction="SELL",
                current_weight=2 / 3,
                target_weight=0.0,
                delta_weight=-2 / 3,
                reference_price=200.0,
                estimated_shares=1.5,
                estimated_notional=300.0,
            ),
        ),
        limits=limits,
        recorder=recorder,
    )

    assert summary is None
    assert any("shares but local snapshot holds" in issue for issue in recorder.issues)

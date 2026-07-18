"""Tests for the paper order-candidate generation command."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from scripts import paper_order_candidates_check as check


def _write_config(
    path: Path,
    *,
    method: str = "equal_weight",
    n_long: int = 2,
    max_position_weight: float = 0.60,
) -> None:
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "name: base_momentum",
                "portfolio:",
                f"  method: {method}",
                f"  n_long: {n_long}",
                f"  max_position_weight: {max_position_weight}",
                "",
            ]
        ),
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

    from tests._research_run_test_helpers import setup_active_research_run

    run_id = setup_active_research_run(engine)
    scores_df = pd.DataFrame(scores)
    scores_df["research_run_id"] = run_id
    scores_df.to_sql("alpha_scores", engine, index=False)
    return engine


def _env() -> dict[str, str]:
    return {"DATABASE_URL": "sqlite://"}


def test_run_generates_sell_first_buy_second_candidates(tmp_path, capsys):
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
        [
            "--strategy-config",
            str(config_path),
            "--strategy-id",
            "v1_base_momentum",
            "--portfolio-input",
            str(portfolio_path),
        ],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper order candidates: OK" in out
    assert "ticker,direction,current_weight,target_weight,delta_weight" in out
    rows = [line for line in out.splitlines() if line.startswith(("AAPL,", "MSFT,", "NVDA,"))]
    assert rows == [
        "NVDA,SELL,0.72222222,0.00000000,-0.72222222,130.000000,10.000000,1300.00",
        "AAPL,BUY,0.22222222,0.50000000,0.27777778,200.000000,2.500000,500.00",
        "MSFT,BUY,0.00000000,0.50000000,0.50000000,450.000000,2.000000,900.00",
    ]


def test_build_order_candidates_returns_empty_when_current_matches_target(tmp_path):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=2)
    strategy_config = check.load_strategy_config(config_path)
    engine = _engine()
    recorder = check.CheckRecorder()
    target = check.construct_target_portfolio(
        engine=engine,
        strategy_config_path=config_path,
        strategy_config=strategy_config,
        strategy_id="v1_base_momentum",
        max_price_age_days=7,
        max_score_age_days=7,
        min_overlap=None,
        today=date(2026, 6, 20),
        recorder=recorder,
    )
    assert target is not None
    snapshot = check.PortfolioSnapshot(
        as_of=date(2026, 6, 20),
        cash=0.0,
        positions=(
            check.CurrentPosition(ticker="AAPL", quantity=2.25, price=200.0),
            check.CurrentPosition(ticker="MSFT", quantity=1.0, price=450.0),
        ),
    )

    candidates = check.build_order_candidates(
        target=target,
        snapshot=snapshot,
        min_delta_weight=check.DEFAULT_MIN_DELTA_WEIGHT,
    )

    assert candidates == ()


def test_load_portfolio_snapshot_rejects_negative_cash(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": -1.0, "positions": []})

    try:
        check.load_portfolio_snapshot(portfolio_path, today=date(2026, 6, 20), max_snapshot_age_days=1)
    except RuntimeError as exc:
        assert "cash must be finite and non-negative" in str(exc)
    else:
        raise AssertionError("Expected negative cash to fail")


def test_load_portfolio_snapshot_rejects_nonpositive_price(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    _write_portfolio(
        portfolio_path,
        {
            "as_of": "2026-06-20",
            "cash": 100.0,
            "positions": [{"ticker": "AAPL", "quantity": 1.0, "price": 0.0}],
        },
    )

    try:
        check.load_portfolio_snapshot(portfolio_path, today=date(2026, 6, 20), max_snapshot_age_days=1)
    except RuntimeError as exc:
        assert "AAPL price must be finite and positive" in str(exc)
    else:
        raise AssertionError("Expected zero price to fail")


def test_load_portfolio_snapshot_rejects_duplicate_ticker(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    _write_portfolio(
        portfolio_path,
        {
            "cash": 100.0,
            "as_of": "2026-06-20",
            "positions": [
                {"ticker": "aapl", "quantity": 1.0, "price": 200.0},
                {"ticker": "AAPL", "quantity": 2.0, "price": 200.0},
            ],
        },
    )

    try:
        check.load_portfolio_snapshot(portfolio_path, today=date(2026, 6, 20), max_snapshot_age_days=1)
    except RuntimeError as exc:
        assert "Duplicate portfolio position ticker: AAPL" in str(exc)
    else:
        raise AssertionError("Expected duplicate ticker to fail")


def test_load_portfolio_snapshot_accepts_utf8_bom(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    portfolio_path.write_text(
        json.dumps(
            {
                "as_of": "2026-06-20",
                "cash": 100.0,
                "positions": [{"ticker": "AAPL", "quantity": 1.0, "price": 200.0}],
            }
        ),
        encoding="utf-8-sig",
    )

    snapshot = check.load_portfolio_snapshot(portfolio_path, today=date(2026, 6, 20), max_snapshot_age_days=1)

    assert snapshot.nav == 300.0
    assert snapshot.as_of == date(2026, 6, 20)
    assert snapshot.positions == (check.CurrentPosition(ticker="AAPL", quantity=1.0, price=200.0),)


def test_load_portfolio_snapshot_requires_as_of(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    _write_portfolio(portfolio_path, {"cash": 100.0, "positions": []})

    try:
        check.load_portfolio_snapshot(portfolio_path, today=date(2026, 6, 20), max_snapshot_age_days=1)
    except RuntimeError as exc:
        assert "must include as_of date" in str(exc)
    else:
        raise AssertionError("Expected missing as_of to fail")


def test_load_portfolio_snapshot_rejects_stale_as_of(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    _write_portfolio(portfolio_path, {"as_of": "2026-06-18", "cash": 100.0, "positions": []})

    try:
        check.load_portfolio_snapshot(portfolio_path, today=date(2026, 6, 20), max_snapshot_age_days=1)
    except RuntimeError as exc:
        assert "Portfolio input as_of 2026-06-18 is stale" in str(exc)
    else:
        raise AssertionError("Expected stale as_of to fail")


def test_load_portfolio_snapshot_rejects_future_as_of(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    _write_portfolio(portfolio_path, {"as_of": "2026-06-21", "cash": 100.0, "positions": []})

    try:
        check.load_portfolio_snapshot(portfolio_path, today=date(2026, 6, 20), max_snapshot_age_days=1)
    except RuntimeError as exc:
        assert "Portfolio input as_of 2026-06-21 is in the future" in str(exc)
    else:
        raise AssertionError("Expected future as_of to fail")


def test_run_fails_without_database_url(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    _write_config(config_path)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 100.0, "positions": []})

    result = check.run(
        [
            "--strategy-config",
            str(config_path),
            "--strategy-id",
            "v1_base_momentum",
            "--portfolio-input",
            str(portfolio_path),
        ],
        env={},
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "DATABASE_URL must be set" in out


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
        [
            "--strategy-config",
            str(config_path),
            "--strategy-id",
            "v1_base_momentum",
            "--portfolio-input",
            str(portfolio_path),
        ],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "alpha_scores date 2026-06-01 is stale" in out


def test_build_order_candidates_rejects_invalid_threshold(tmp_path):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=2)
    strategy_config = check.load_strategy_config(config_path)
    engine = _engine()
    recorder = check.CheckRecorder()
    target = check.construct_target_portfolio(
        engine=engine,
        strategy_config_path=config_path,
        strategy_config=strategy_config,
        strategy_id="v1_base_momentum",
        max_price_age_days=7,
        max_score_age_days=7,
        min_overlap=None,
        today=date(2026, 6, 20),
        recorder=recorder,
    )
    assert target is not None

    try:
        check.build_order_candidates(
            target=target,
            snapshot=check.PortfolioSnapshot(as_of=date(2026, 6, 20), cash=100.0, positions=()),
            min_delta_weight=float("nan"),
        )
    except RuntimeError as exc:
        assert "--min-delta-weight must be finite and non-negative" in str(exc)
    else:
        raise AssertionError("Expected invalid threshold to fail")

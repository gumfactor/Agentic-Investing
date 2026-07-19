"""Tests for the paper target-portfolio construction command."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from scripts import paper_target_check as check


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


def _engine(
    *,
    price_date: str = "2026-06-19",
    score_date: str = "2026-06-19",
    strategy_id: str = "v1_base_momentum",
    scores: list[dict] | None = None,
):
    engine = create_engine("sqlite://")
    prices = [
        {"ticker": "AAPL", "date": price_date, "close": 200.0},
        {"ticker": "MSFT", "date": price_date, "close": 450.0},
        {"ticker": "NVDA", "date": price_date, "close": 130.0},
    ]
    score_rows = scores or [
        {"ticker": "AAPL", "score_date": score_date, "strategy_id": strategy_id, "alpha_score": 0.8, "rank": 2, "universe_size": 3},
        {"ticker": "MSFT", "score_date": score_date, "strategy_id": strategy_id, "alpha_score": 1.2, "rank": 1, "universe_size": 3},
        {"ticker": "NVDA", "score_date": score_date, "strategy_id": strategy_id, "alpha_score": 0.4, "rank": 3, "universe_size": 3},
    ]
    pd.DataFrame(prices).to_sql("daily_prices", engine, index=False)

    from tests._research_run_test_helpers import setup_active_research_run

    run_id = setup_active_research_run(engine)
    scores_df = pd.DataFrame(score_rows)
    scores_df["research_run_id"] = run_id
    scores_df.to_sql("alpha_scores", engine, index=False)
    return engine


def _env() -> dict[str, str]:
    return {"DATABASE_URL": "sqlite://"}


def test_run_constructs_equal_weight_targets_from_top_scores(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    engine = _engine()

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1_base_momentum"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper target: OK" in out
    assert "MSFT,0.50000000,450.000000,1.20000000" in out
    assert "AAPL,0.50000000,200.000000,0.80000000" in out
    assert "NVDA" not in out
    assert "Cash residual weight: 0.000000" in out


def test_construct_equal_weight_targets_leaves_cash_when_position_cap_binds(tmp_path):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=2, max_position_weight=0.40)
    strategy_config = check.load_strategy_config(config_path)
    engine = _engine()
    price_rows = check._load_latest_prices(engine)[1]
    score_rows = check._load_latest_scores(engine, "v1_base_momentum")[1]

    target = check.construct_equal_weight_targets(
        strategy_id="v1_base_momentum",
        config=strategy_config,
        price_rows=price_rows,
        score_rows=score_rows,
        as_of_date=date(2026, 6, 19),
    )

    assert [pos.ticker for pos in target.positions] == ["MSFT", "AAPL"]
    assert [pos.target_weight for pos in target.positions] == [0.4, 0.4]
    assert target.cash_weight == 0.19999999999999996


def test_construct_equal_weight_targets_uses_rank_to_break_equal_score_ties(tmp_path):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=2)
    strategy_config = check.load_strategy_config(config_path)

    target = check.construct_equal_weight_targets(
        strategy_id="v1_base_momentum",
        config=strategy_config,
        price_rows=[
            {"ticker": "AAA", "date": "2026-06-19", "close": 10.0},
            {"ticker": "BBB", "date": "2026-06-19", "close": 20.0},
            {"ticker": "CCC", "date": "2026-06-19", "close": 30.0},
        ],
        score_rows=[
            {"ticker": "AAA", "score_date": "2026-06-19", "alpha_score": 1.0, "rank": 3},
            {"ticker": "BBB", "score_date": "2026-06-19", "alpha_score": 1.0, "rank": 1},
            {"ticker": "CCC", "score_date": "2026-06-19", "alpha_score": 1.0, "rank": 2},
        ],
        as_of_date=date(2026, 6, 19),
    )

    assert [pos.ticker for pos in target.positions] == ["BBB", "CCC"]


def test_run_fails_without_database_url(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1_base_momentum"],
        env={},
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "DATABASE_URL must be set" in out


def test_run_requires_explicit_strategy_id(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)

    result = check.run(["--strategy-config", str(config_path)], env=_env())

    out = capsys.readouterr().out
    assert result == 1
    assert "--strategy-id must be passed explicitly" in out


def test_run_fails_when_step_two_input_gate_fails(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine(score_date="2026-06-01")

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1_base_momentum"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "alpha_scores date 2026-06-01 is stale" in out


def test_run_fails_when_scores_are_newer_than_latest_prices(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine(price_date="2026-06-18", score_date="2026-06-19")

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1_base_momentum"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "alpha_scores date 2026-06-19 is newer than latest daily_prices date 2026-06-18" in out


def test_run_rejects_unsupported_portfolio_method(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, method="mvo")
    engine = _engine()

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1_base_momentum"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Portfolio method 'mvo' is not supported" in out


def test_run_rejects_invalid_max_position_weight(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, max_position_weight=0.0)
    engine = _engine()

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1_base_momentum"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "portfolio.max_position_weight must be in (0, 1]" in out


def test_construct_equal_weight_targets_rejects_missing_top_price(tmp_path):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=2)
    strategy_config = check.load_strategy_config(config_path)

    try:
        check.construct_equal_weight_targets(
            strategy_id="v1_base_momentum",
            config=strategy_config,
            price_rows=[{"ticker": "LOW", "date": "2026-06-19", "close": 10.0}],
            score_rows=[
                {"ticker": "TOP", "score_date": "2026-06-19", "alpha_score": 2.0},
                {"ticker": "LOW", "score_date": "2026-06-19", "alpha_score": 1.0},
            ],
            as_of_date=date(2026, 6, 19),
        )
    except RuntimeError as exc:
        assert "Top scored tickers missing latest prices: TOP" in str(exc)
    else:
        raise AssertionError("Expected missing top price to fail")

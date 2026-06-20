"""Tests for the paper strategy-input preflight command."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

from scripts import paper_inputs_check as check


def _write_config(path: Path, *, version: int = 1, n_long: int = 2, strategy_id: str | None = None) -> None:
    strategy_id_line = f"strategy_id: {strategy_id}\n" if strategy_id is not None else ""
    path.write_text(
        "\n".join(
            [
                f"version: {version}",
                "name: base_momentum",
                strategy_id_line.rstrip(),
                "portfolio:",
                f"  n_long: {n_long}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _engine(
    *,
    price_date: str = "2026-06-19",
    score_date: str = "2026-06-19",
    strategy_id: str = "v1",
    prices: list[dict] | None = None,
    scores: list[dict] | None = None,
):
    engine = create_engine("sqlite://")
    price_rows = prices or [
        {"ticker": "AAPL", "date": price_date, "close": 200.0},
        {"ticker": "MSFT", "date": price_date, "close": 450.0},
        {"ticker": "NVDA", "date": price_date, "close": 130.0},
    ]
    score_rows = scores or [
        {"ticker": "AAPL", "score_date": score_date, "strategy_id": strategy_id, "alpha_score": 1.5, "rank": 1, "universe_size": 3},
        {"ticker": "MSFT", "score_date": score_date, "strategy_id": strategy_id, "alpha_score": 1.0, "rank": 2, "universe_size": 3},
        {"ticker": "NVDA", "score_date": score_date, "strategy_id": strategy_id, "alpha_score": 0.5, "rank": 3, "universe_size": 3},
    ]
    pd.DataFrame(price_rows).to_sql("daily_prices", engine, index=False)
    pd.DataFrame(score_rows).to_sql("alpha_scores", engine, index=False)
    return engine


def _env(url: str = "sqlite://") -> dict[str, str]:
    return {"DATABASE_URL": url}


def test_run_passes_with_recent_prices_scores_and_overlap(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine()

    result = check.run(
        [
            "--strategy-config",
            str(config_path),
            "--strategy-id",
            "v1",
        ],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper inputs: OK" in out
    assert "strategy_id='v1'" in out
    assert "Top scored tickers: AAPL, MSFT, NVDA" in out


def test_run_requires_database_url(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)

    result = check.run(["--strategy-config", str(config_path), "--strategy-id", "v1"], env={})

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


def test_run_fails_when_config_is_missing(capsys):
    result = check.run(
        ["--strategy-config", "missing.yaml", "--strategy-id", "v1"],
        env=_env(),
        engine_factory=lambda _url: _engine(),
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Strategy config not found" in out


def test_resolve_strategy_id_prefers_explicit_then_config_then_version():
    assert check.resolve_strategy_id({"version": 1, "strategy_id": "yaml_id"}, "cli_id") == "cli_id"
    assert check.resolve_strategy_id({"version": 1, "strategy_id": "yaml_id"}, None) == "yaml_id"
    assert check.resolve_strategy_id({"version": 2, "name": "display_name"}, None) == "v2"


def test_run_fails_when_no_scores_exist_for_strategy_id(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine(strategy_id="v1_base_momentum")

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "No alpha_scores found for strategy_id='v1'" in out


def test_run_fails_for_stale_price_or_score_dates(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine(price_date="2026-06-01", score_date="2026-06-02")

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1", "--max-price-age-days", "7", "--max-score-age-days", "7"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "daily_prices date 2026-06-01 is stale" in out
    assert "alpha_scores date 2026-06-02 is stale" in out


def test_run_fails_for_future_price_or_score_dates(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine(price_date="2026-06-21", score_date="2026-06-22")

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "daily_prices date 2026-06-21 is in the future" in out
    assert "alpha_scores date 2026-06-22 is in the future" in out


def test_run_fails_for_negative_max_age(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine()

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1", "--max-price-age-days", "-1"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "daily_prices max age must be non-negative" in out


def test_run_fails_for_invalid_latest_price_close(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine(prices=[
        {"ticker": "AAPL", "date": "2026-06-19", "close": 200.0},
        {"ticker": "MSFT", "date": "2026-06-19", "close": 0.0},
    ])

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "invalid close for MSFT" in out


def test_run_fails_for_invalid_alpha_score(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine(scores=[
        {"ticker": "AAPL", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": 1.0, "rank": 1, "universe_size": 2},
        {"ticker": "MSFT", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": None, "rank": 2, "universe_size": 2},
    ])

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "invalid alpha_score for MSFT" in out


def test_run_fails_when_overlap_is_below_required_long_book(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=2)
    engine = _engine(
        prices=[
            {"ticker": "AAPL", "date": "2026-06-19", "close": 200.0},
        ],
        scores=[
            {"ticker": "AAPL", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": 1.0, "rank": 1, "universe_size": 3},
            {"ticker": "MSFT", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": 0.9, "rank": 2, "universe_size": 3},
            {"ticker": "NVDA", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": 0.8, "rank": 3, "universe_size": 3},
        ],
    )

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Only 1 scored tickers have latest prices; need at least 2" in out


def test_run_fails_when_top_scored_tickers_lack_latest_prices(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=2)
    engine = _engine(
        prices=[
            {"ticker": "LOW1", "date": "2026-06-19", "close": 50.0},
            {"ticker": "LOW2", "date": "2026-06-19", "close": 60.0},
        ],
        scores=[
            {"ticker": "TOP1", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": 2.0, "rank": 1, "universe_size": 4},
            {"ticker": "TOP2", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": 1.9, "rank": 2, "universe_size": 4},
            {"ticker": "LOW1", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": 0.2, "rank": 3, "universe_size": 4},
            {"ticker": "LOW2", "score_date": "2026-06-19", "strategy_id": "v1", "alpha_score": 0.1, "rank": 4, "universe_size": 4},
        ],
    )

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Top 2 scored tickers missing latest prices: TOP1, TOP2" in out


def test_run_fails_for_non_positive_min_overlap(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path)
    engine = _engine()

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1", "--min-overlap", "0"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Minimum overlap must be positive" in out


def test_run_allows_min_overlap_override_for_smoke_tests(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    _write_config(config_path, n_long=50)
    engine = _engine()

    result = check.run(
        ["--strategy-config", str(config_path), "--strategy-id", "v1", "--min-overlap", "3"],
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    assert result == 0

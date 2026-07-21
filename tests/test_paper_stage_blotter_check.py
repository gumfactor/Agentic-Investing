"""Tests for the paper stage-only blotter artifact command."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from scripts import paper_stage_blotter_check as check

# NOTE (BUG-081): the env/cwd isolation fixture that used to live here
# (added under BUG-080) has been superseded by the shared, autouse
# `_paper_path_isolate_global_state` fixture in `tests/conftest.py`, which
# applies the same snapshot/restore behavior to every `tests/test_paper_*.py`
# module rather than just this one. See conftest.py for the full rationale.


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


def _pass_args(config_path: Path, portfolio_path: Path, output_path: Path) -> list[str]:
    return [
        "--strategy-config",
        str(config_path),
        "--strategy-id",
        "v1_base_momentum",
        "--portfolio-input",
        str(portfolio_path),
        "--output",
        str(output_path),
    ]


def test_run_writes_stage_only_blotter_after_step_five_passes(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "artifacts" / "paper_stage_blotter.json"
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
        _pass_args(config_path, portfolio_path, output_path),
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
        now_fn=lambda: datetime(2026, 6, 20, 14, 30, tzinfo=UTC),
        run_id_factory=lambda: "run-step-6-test",
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper stage-only blotter: OK" in out
    assert output_path.exists()

    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    rows = artifact["candidate_rows"]
    assert artifact["schema_version"] == "paper_stage_blotter.v1"
    assert artifact["run_id"] == "run-step-6-test"
    assert artifact["generated_at_utc"] == "2026-06-20T14:30:00+00:00"
    assert artifact["paper_only"] is True
    assert artifact["stage_only"] is True
    assert artifact["provenance"]["strategy_config_sha256"] == check._file_sha256(config_path)
    assert artifact["provenance"]["portfolio_input_sha256"] == check._file_sha256(portfolio_path)
    assert artifact["provenance"]["gate_inputs_sha256"] == check._stable_sha256(
        artifact["provenance"]["gate_inputs"]
    )
    assert artifact["source"] == {
        "step5_required": True,
        "target_as_of_date": "2026-06-19",
        "portfolio_snapshot_as_of": "2026-06-20",
    }
    assert artifact["safety"] == {
        "broker_connected": False,
        "broker_order_ids_present": False,
        "order_manager_registered": False,
        "orders_submitted": False,
        "orders_cancelled": False,
        "fills_reconciled": False,
        "human_yes_consumed": False,
    }
    assert artifact["risk_compliance_summary"]["candidate_count"] == 3
    assert artifact["risk_compliance_summary"]["turnover_weight"] == 2600.0 / 1800.0
    assert artifact["rounding_summary"]["quantity_mode"] == "whole_shares"
    assert artifact["rounding_summary"]["original_candidate_count"] == 3
    assert artifact["rounding_summary"]["rounded_candidate_count"] == 3
    assert artifact["rounding_summary"]["dropped_zero_share_count"] == 0
    assert artifact["rounding_summary"]["residual_cash_from_rounding"] == 100.0
    assert artifact["candidate_rows_sha256"] == check._rows_checksum(rows)
    assert artifact["artifact_sha256"] == check._artifact_checksum(artifact)
    assert [row["ticker"] for row in rows] == ["NVDA", "AAPL", "MSFT"]
    assert [row["estimated_shares"] for row in rows] == [10.0, 2.0, 2.0]
    assert [row["estimated_notional"] for row in rows] == [1300.0, 400.0, 900.0]
    assert all(row["review_status"] == "LOCAL_STAGE_ONLY" for row in rows)
    assert all("broker" not in row for row in rows)


def test_whole_share_rounding_drops_zero_share_orders(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "paper_stage_blotter.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 100.0, "positions": []})

    result = check.run(
        _pass_args(config_path, portfolio_path, output_path),
        env=_env(),
        engine_factory=lambda _url: _engine(),
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Whole-share rounding dropped all order candidates" in out
    assert not output_path.exists()


def test_whole_share_rounding_fails_when_only_some_legs_drop(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "paper_stage_blotter.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(
        portfolio_path,
        {
            "as_of": "2026-06-20",
            "cash": 0.0,
            "positions": [{"ticker": "NVDA", "quantity": 1.0, "price": 130.0}],
        },
    )

    result = check.run(
        _pass_args(config_path, portfolio_path, output_path),
        env=_env(),
        engine_factory=lambda _url: _engine(),
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Whole-share rounding dropped some order candidates" in out
    assert not output_path.exists()


def test_api_limit_price_rounding_is_side_aware():
    assert check._api_limit_price(434.459991, "BUY") == 434.46
    assert check._api_limit_price(434.451, "BUY") == 434.46
    assert check._api_limit_price(434.459991, "SELL") == 434.45
    assert check._api_limit_price(434.451, "SELL") == 434.45


def test_fractional_mode_is_explicit_and_records_metadata(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "paper_stage_blotter.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 1000.0, "positions": []})

    result = check.run(
        [*_pass_args(config_path, portfolio_path, output_path), "--allow-fractional-shares"],
        env=_env(),
        engine_factory=lambda _url: _engine(),
        today_fn=lambda: date(2026, 6, 20),
    )

    assert result == 0
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["rounding_summary"]["quantity_mode"] == "fractional"
    assert any(row["estimated_shares"] != round(row["estimated_shares"]) for row in artifact["candidate_rows"])


def test_run_refuses_to_overwrite_existing_artifact_without_flag(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "paper_stage_blotter.json"
    _write_config(config_path)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 100.0, "positions": []})
    output_path.write_text("do not replace", encoding="utf-8")

    result = check.run(
        _pass_args(config_path, portfolio_path, output_path),
        env=_env(),
        engine_factory=lambda _url: _engine(),
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Pass --overwrite to replace it" in out
    assert output_path.read_text(encoding="utf-8") == "do not replace"


def test_write_artifact_non_overwrite_does_not_clobber_if_target_appears(tmp_path, monkeypatch):
    output_path = tmp_path / "paper_stage_blotter.json"
    artifact = {"schema_version": "test"}
    real_link = check.os.link

    def racing_link(src, dst):
        Path(dst).write_text("racing writer", encoding="utf-8")
        return real_link(src, dst)

    monkeypatch.setattr(check.os, "link", racing_link)

    try:
        check._write_artifact(output_path, artifact, overwrite=False)
    except RuntimeError as exc:
        assert "Pass --overwrite" in str(exc)
    else:
        raise AssertionError("Expected racing no-clobber write to fail")

    assert output_path.read_text(encoding="utf-8") == "racing writer"


def test_run_overwrites_existing_artifact_with_flag(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "paper_stage_blotter.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 1000.0, "positions": []})
    output_path.write_text("replace me", encoding="utf-8")

    result = check.run(
        [*_pass_args(config_path, portfolio_path, output_path), "--overwrite"],
        env=_env(),
        engine_factory=lambda _url: _engine(),
        today_fn=lambda: date(2026, 6, 20),
        now_fn=lambda: datetime(2026, 6, 20, tzinfo=UTC),
        run_id_factory=lambda: "overwrite-test",
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper stage-only blotter: OK" in out
    assert json.loads(output_path.read_text(encoding="utf-8"))["run_id"] == "overwrite-test"


def test_run_does_not_write_when_step_five_gate_fails(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "paper_stage_blotter.json"
    _write_config(config_path, n_long=2, max_position_weight=0.60)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 1000.0, "positions": []})
    engine = _engine(score_date="2026-06-01")

    result = check.run(
        _pass_args(config_path, portfolio_path, output_path),
        env=_env(),
        engine_factory=lambda _url: engine,
        today_fn=lambda: date(2026, 6, 20),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "alpha_scores date 2026-06-01 is stale" in out
    assert not output_path.exists()


def test_run_requires_explicit_strategy_id(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "paper_stage_blotter.json"
    _write_config(config_path)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 100.0, "positions": []})

    result = check.run(
        [
            "--strategy-config",
            str(config_path),
            "--portfolio-input",
            str(portfolio_path),
            "--output",
            str(output_path),
        ],
        env=_env(),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "--strategy-id must be passed explicitly" in out
    assert not output_path.exists()


def test_run_rejects_live_clearance_flag(tmp_path, capsys):
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "paper_stage_blotter.json"
    _write_config(config_path)
    _write_portfolio(portfolio_path, {"as_of": "2026-06-20", "cash": 100.0, "positions": []})

    result = check.run(
        _pass_args(config_path, portfolio_path, output_path),
        env={"DATABASE_URL": "sqlite://", "PAPER_RUN_CLEARED": "true"},
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "PAPER_RUN_CLEARED=true" in out
    assert not output_path.exists()


def test_step_six_does_not_directly_import_broker_order_manager_or_order_dto():
    source = Path(check.__file__).read_text(encoding="utf-8")

    assert "from execution.brokers" not in source
    assert "import execution.brokers" not in source
    assert "from execution.oms.order_manager" not in source
    assert "import execution.oms.order_manager" not in source
    assert "OrderManager(" not in source
    assert ".stage(" not in source
    assert "from execution.oms.order" not in source

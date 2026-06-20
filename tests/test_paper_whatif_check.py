"""Tests for the Step 7.5 IBKR paper what-if validation command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from execution.oms.order import Order
from scripts import paper_stage_blotter_check as stage
from scripts import paper_whatif_check as check


def _env() -> dict[str, str]:
    return {"PAPER_TRADING": "true", "IBKR_PORT": "7497"}


def _write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _write_blotter(tmp_path: Path, *, mutate: Any = None) -> Path:
    config_path = tmp_path / "strategy.yaml"
    portfolio_path = tmp_path / "portfolio.json"
    blotter_path = tmp_path / "paper_stage_blotter.json"
    _write_file(config_path, "version: 1\nname: base_momentum\n")
    _write_file(portfolio_path, json.dumps({"as_of": "2026-06-20", "cash": 1000.0, "positions": []}))
    rows = [
        {
            "sequence": 1,
            "ticker": "AAPL",
            "direction": "BUY",
            "review_status": "LOCAL_STAGE_ONLY",
            "current_weight": 0.0,
            "target_weight": 0.5,
            "delta_weight": 0.5,
            "reference_price": 200.0,
            "estimated_shares": 2.5,
            "estimated_notional": 500.0,
        },
        {
            "sequence": 2,
            "ticker": "MSFT",
            "direction": "BUY",
            "review_status": "LOCAL_STAGE_ONLY",
            "current_weight": 0.0,
            "target_weight": 0.5,
            "delta_weight": 0.5,
            "reference_price": 450.0,
            "estimated_shares": 1.25,
            "estimated_notional": 562.5,
        },
    ]
    artifact = {
        "schema_version": "paper_stage_blotter.v1",
        "artifact_type": "paper_stage_only_order_blotter",
        "run_id": "step-6-run",
        "generated_at_utc": "2026-06-20T14:30:00+00:00",
        "paper_only": True,
        "stage_only": True,
        "strategy_id": "v1_base_momentum",
        "strategy_config": str(config_path),
        "provenance": {
            "strategy_config_path": str(config_path),
            "strategy_config_sha256": stage._file_sha256(config_path),
            "portfolio_input_path": str(portfolio_path),
            "portfolio_input_sha256": stage._file_sha256(portfolio_path),
            "gate_inputs": {
                "target_as_of_date": "2026-06-19",
                "portfolio_snapshot_as_of": "2026-06-20",
                "max_position_weight": 0.6,
                "max_gross_target_weight": 1.0,
                "allow_shorts": False,
                "max_turnover_weight": None,
                "min_order_notional": 0.0,
            },
        },
        "source": {
            "step5_required": True,
            "target_as_of_date": "2026-06-19",
            "portfolio_snapshot_as_of": "2026-06-20",
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
        "risk_compliance_summary": {"candidate_count": 2},
        "candidate_rows_sha256": stage._rows_checksum(rows),
        "candidate_rows": rows,
        "output_path": str(blotter_path),
    }
    artifact["provenance"]["gate_inputs_sha256"] = stage._stable_sha256(
        artifact["provenance"]["gate_inputs"]
    )
    if mutate is not None:
        mutate(artifact)
    artifact["artifact_sha256"] = stage._artifact_checksum(artifact)
    blotter_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    return blotter_path


class FakeWhatIfBroker:
    def __init__(self, *, paper: bool = True, port: int = 7497, reject_ticker: str | None = None) -> None:
        self._paper = paper
        self._port = port
        self.reject_ticker = reject_ticker
        self.connected = False
        self.disconnected = False
        self.what_if_orders: list[Order] = []

    @property
    def is_paper(self) -> bool:
        return self._paper

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def what_if_order(self, order: Order) -> dict[str, Any]:
        self.what_if_orders.append(order)
        if order.ticker == self.reject_ticker:
            raise RuntimeError("fractional quantity rejected")
        return {
            "status": "PreSubmitted",
            "warning_text": "",
            "commission": "1.00",
            "commission_currency": "USD",
            "init_margin_change": "500.00",
            "maint_margin_change": "250.00",
        }

    def submit_order(self, order: Order) -> str:
        raise AssertionError("what-if validation must not submit orders")


def test_writes_successful_fractional_whatif_artifact(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_whatif.json"
    fake = FakeWhatIfBroker()

    result = check.run(
        ["--blotter", str(blotter_path), "--output", str(output_path), "--client-id", "11"],
        env=_env(),
        broker_factory=lambda client_id: fake,
        now_fn=lambda: datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
        run_id_factory=lambda: "whatif-run",
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper what-if validation: OK" in out
    assert fake.connected is True
    assert fake.disconnected is True
    assert [(order.ticker, order.quantity) for order in fake.what_if_orders] == [
        ("AAPL", 2.5),
        ("MSFT", 1.25),
    ]

    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == "paper_whatif_validation.v1"
    assert artifact["status"] == "PASS"
    assert artifact["paper_only"] is True
    assert artifact["transmit_orders"] is False
    assert artifact["human_yes_consumed"] is False
    assert artifact["accepted_count"] == 2
    assert artifact["rejected_count"] == 0
    assert artifact["fractional_quantity_count"] == 2
    assert artifact["source_blotter_sha256"] == stage._file_sha256(blotter_path)
    assert [row["accepted"] for row in artifact["results"]] == [True, True]
    assert artifact["artifact_sha256"] == check._artifact_checksum(artifact)


def test_records_broker_rejects_without_stopping_remaining_rows(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_whatif.json"
    fake = FakeWhatIfBroker(reject_ticker="AAPL")

    result = check.run(
        ["--blotter", str(blotter_path), "--output", str(output_path)],
        env=_env(),
        broker_factory=lambda client_id: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Paper what-if validation: FAILED" in out
    assert "1 AAPL BUY: fractional quantity rejected" in out
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "FAILED"
    assert artifact["accepted_count"] == 1
    assert artifact["rejected_count"] == 1
    assert [row["accepted"] for row in artifact["results"]] == [False, True]
    assert fake.disconnected is True


def test_requires_paper_environment(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_whatif.json"

    result = check.run(
        ["--blotter", str(blotter_path), "--output", str(output_path)],
        env={"PAPER_TRADING": "true", "IBKR_PORT": "7496"},
        broker_factory=lambda client_id: FakeWhatIfBroker(),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "IBKR_PORT must be exactly 7497" in out
    assert not output_path.exists()


def test_rejects_non_paper_broker_before_connection(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_whatif.json"
    fake = FakeWhatIfBroker(paper=False)

    result = check.run(
        ["--blotter", str(blotter_path), "--output", str(output_path)],
        env=_env(),
        broker_factory=lambda client_id: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "did not report paper mode before connection" in out
    assert fake.connected is False
    assert not output_path.exists()


def test_refuses_to_overwrite_without_flag(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_whatif.json"
    output_path.write_text("keep me", encoding="utf-8")

    result = check.run(
        ["--blotter", str(blotter_path), "--output", str(output_path)],
        env=_env(),
        broker_factory=lambda client_id: FakeWhatIfBroker(),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Pass --overwrite" in out
    assert output_path.read_text(encoding="utf-8") == "keep me"


def test_source_does_not_submit_cancel_reconcile_or_consume_yes():
    source = Path(check.__file__).read_text(encoding="utf-8")

    assert ".submit_order(" not in source
    assert ".cancel" not in source
    assert ".get_fill(" not in source
    assert "--confirm" not in source
    assert 'args.confirm' not in source

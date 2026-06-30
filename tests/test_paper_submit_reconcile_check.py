"""Tests for the Step 7 paper submit/reconcile preflight command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from execution.oms.order import Order
from scripts import paper_stage_blotter_check as stage
from scripts import paper_submit_reconcile_check as check


def _env() -> dict[str, str]:
    return {"PAPER_TRADING": "true", "IBKR_PORT": "7497"}


def _write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _write_blotter(tmp_path: Path, *, mutate: Any = None, generated_at_utc: str | None = None) -> Path:
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
            "target_weight": 0.4,
            "delta_weight": 0.4,
            "reference_price": 200.0,
            "estimated_shares": 2.0,
            "estimated_notional": 400.0,
        },
        {
            "sequence": 2,
            "ticker": "MSFT",
            "direction": "SELL",
            "review_status": "LOCAL_STAGE_ONLY",
            "current_weight": 0.7,
            "target_weight": 0.2,
            "delta_weight": -0.5,
            "reference_price": 450.0,
            "estimated_shares": 1.0,
            "estimated_notional": 450.0,
        },
    ]
    # Default to now so freshness checks pass without an explicit override
    ts = generated_at_utc or datetime.now(UTC).isoformat()
    artifact = {
        "schema_version": "paper_stage_blotter.v1",
        "artifact_type": "paper_stage_only_order_blotter",
        "run_id": "step-6-run",
        "generated_at_utc": ts,
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


class FakeBroker:
    def __init__(self, *, paper: bool = True, port: int = 7497) -> None:
        self._paper = paper
        self._port = port
        self.connected = False
        self.disconnected = False
        self.orders: list[Order] = []

    @property
    def is_paper(self) -> bool:
        return self._paper

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def submit_order(self, order: Order) -> str:
        self.orders.append(order)
        return f"paper-{len(self.orders)}"

    def get_fill(self, broker_order_id: str) -> dict[str, Any] | None:
        return {"status": "Submitted", "filled_quantity": 0.0, "avg_price": 0.0}


class FlipsAfterConnectBroker(FakeBroker):
    def connect(self) -> None:
        super().connect()
        self._paper = False


class FailsOnSecondOrderBroker(FakeBroker):
    def submit_order(self, order: Order) -> str:
        if len(self.orders) == 1:
            raise RuntimeError("simulated second-order failure")
        return super().submit_order(order)


def test_dry_run_validates_and_displays_without_broker_or_output(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    called = False

    def broker_factory() -> FakeBroker:
        nonlocal called
        called = True
        return FakeBroker()

    result = check.run(["--blotter", str(blotter_path)], env=_env(), broker_factory=broker_factory)

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper submit/reconcile preflight: DRY-RUN OK" in out
    assert "Full paper order list" in out
    assert "Reviewed blotter sha256" in out
    assert "AAPL" in out
    assert called is False


def test_requires_paper_environment_even_for_dry_run(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)

    result = check.run(
        ["--blotter", str(blotter_path)],
        env={"PAPER_TRADING": "true", "IBKR_PORT": "7496"},
        broker_factory=FakeBroker,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "IBKR_PORT must be exactly 7497" in out


def test_rejects_live_clearance_flag(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)

    result = check.run(
        ["--blotter", str(blotter_path)],
        env={"PAPER_TRADING": "true", "IBKR_PORT": "7497", "PAPER_RUN_CLEARED": "true"},
        broker_factory=FakeBroker,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "PAPER_RUN_CLEARED=true" in out


def test_rejects_non_literal_confirmation(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)

    result = check.run(
        ["--blotter", str(blotter_path), "--confirm", "yes"],
        env=_env(),
        broker_factory=FakeBroker,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "literal string" in out


def test_submit_requires_separate_output(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(blotter_path),
        ],
        env=_env(),
        broker_factory=FakeBroker,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "must be separate" in out


def test_submit_requires_reviewed_blotter_checksum(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_reconciliation.json"

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            "0" * 64,
            "--output",
            str(output_path),
        ],
        env=_env(),
        broker_factory=FakeBroker,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "reviewed-blotter-sha256 must match" in out
    assert not output_path.exists()


def test_rejects_blotter_with_submitted_status(tmp_path, capsys):
    def mutate(artifact: dict[str, Any]) -> None:
        artifact["candidate_rows"][0]["review_status"] = "SUBMITTED"
        artifact["candidate_rows_sha256"] = stage._rows_checksum(artifact["candidate_rows"])

    blotter_path = _write_blotter(tmp_path, mutate=mutate)

    result = check.run(["--blotter", str(blotter_path)], env=_env(), broker_factory=FakeBroker)

    out = capsys.readouterr().out
    assert result == 1
    assert "review_status must be LOCAL_STAGE_ONLY" in out


def test_rejects_blotter_with_broker_id(tmp_path, capsys):
    def mutate(artifact: dict[str, Any]) -> None:
        artifact["candidate_rows"][0]["broker_order_id"] = "123"
        artifact["candidate_rows_sha256"] = stage._rows_checksum(artifact["candidate_rows"])

    blotter_path = _write_blotter(tmp_path, mutate=mutate)

    result = check.run(["--blotter", str(blotter_path)], env=_env(), broker_factory=FakeBroker)

    out = capsys.readouterr().out
    assert result == 1
    assert "forbidden broker/order field" in out


def test_rejects_checksum_mismatch(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    artifact = json.loads(blotter_path.read_text(encoding="utf-8"))
    artifact["candidate_rows"][0]["ticker"] = "TSLA"
    blotter_path.write_text(json.dumps(artifact), encoding="utf-8")

    result = check.run(["--blotter", str(blotter_path)], env=_env(), broker_factory=FakeBroker)

    out = capsys.readouterr().out
    assert result == 1
    assert "candidate_rows_sha256 mismatch" in out


def test_confirm_yes_submits_with_fake_broker_and_writes_reconciliation(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FakeBroker()

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env=_env(),
        broker_factory=lambda: fake,
        now_fn=lambda: datetime(2026, 6, 20, 15, 0, tzinfo=UTC),
        run_id_factory=lambda: "step-7-run",
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper submit/reconcile preflight: SUBMITTED" in out
    assert fake.connected is True
    assert fake.disconnected is True
    assert [(order.ticker, order.side.value, order.quantity, order.limit_price) for order in fake.orders] == [
        ("AAPL", "BUY", 2.0, 200.0),
        ("MSFT", "SELL", 1.0, 450.0),
    ]

    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == "paper_submit_reconcile.v1"
    assert artifact["run_id"] == "step-7-run"
    assert artifact["paper_only"] is True
    assert artifact["status"] == "SUBMITTED"
    assert artifact["live_port_supported"] is False
    assert artifact["source_blotter_path"] == str(blotter_path)
    assert artifact["source_blotter_sha256"] == stage._file_sha256(blotter_path)
    assert artifact["order_count"] == 2
    assert [row["broker_order_id"] for row in artifact["broker_responses"]] == ["paper-1", "paper-2"]
    assert artifact["safety"] == {
        "operator_confirmed_yes": True,
        "paper_env_required": True,
        "ibkr_port": 7497,
        "orders_cancelled": False,
        "circuit_breaker_reset": False,
        "live_orders_allowed": False,
    }
    assert artifact["artifact_sha256"] == check._reconciliation_checksum(artifact)


def test_confirm_yes_passes_configured_client_id_to_broker_factory(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FakeBroker()
    client_ids: list[int | None] = []

    def broker_factory(client_id: int | None) -> FakeBroker:
        client_ids.append(client_id)
        return fake

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env={**_env(), "IBKR_CLIENT_ID": "7"},
        broker_factory=broker_factory,
    )

    assert result == 0
    assert client_ids == [7]


def test_confirm_yes_rejects_invalid_configured_client_id(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FakeBroker()

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env={**_env(), "IBKR_CLIENT_ID": "not-an-int"},
        broker_factory=lambda: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "IBKR_CLIENT_ID must be an integer" in out
    assert fake.connected is False
    assert not output_path.exists()


def test_confirm_yes_rejects_fractional_quantities_before_broker_connection(tmp_path, capsys):
    def mutate(artifact: dict[str, Any]) -> None:
        artifact["candidate_rows"][0]["estimated_shares"] = 2.5
        artifact["candidate_rows"][0]["estimated_notional"] = 500.0
        artifact["candidate_rows_sha256"] = stage._rows_checksum(artifact["candidate_rows"])

    blotter_path = _write_blotter(tmp_path, mutate=mutate)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FakeBroker()

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env=_env(),
        broker_factory=lambda: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "IBKR TWS API rejects fractional-sized stock orders" in out
    assert fake.connected is False
    assert not output_path.exists()


def test_confirm_yes_rejects_sub_cent_limit_prices_before_broker_connection(tmp_path, capsys):
    def mutate(artifact: dict[str, Any]) -> None:
        artifact["candidate_rows"][0]["reference_price"] = 200.001
        artifact["candidate_rows"][0]["estimated_notional"] = 400.002
        artifact["candidate_rows_sha256"] = stage._rows_checksum(artifact["candidate_rows"])

    blotter_path = _write_blotter(tmp_path, mutate=mutate)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FakeBroker()

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env=_env(),
        broker_factory=lambda: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "sub-cent stock limit prices" in out
    assert fake.connected is False
    assert not output_path.exists()


def test_partial_submission_failure_writes_attempt_artifact(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FailsOnSecondOrderBroker()

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env=_env(),
        broker_factory=lambda: fake,
        now_fn=lambda: datetime(2026, 6, 20, 15, 0, tzinfo=UTC),
        run_id_factory=lambda: "step-7-partial",
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Paper submission failed; reconciliation attempt artifact written" in out
    assert output_path.exists()
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "FAILED"
    assert artifact["run_id"] == "step-7-partial"
    assert artifact["last_attempted_sequence"] == 2
    assert "simulated second-order failure" in artifact["error"]
    assert [row["broker_order_id"] for row in artifact["broker_responses"]] == ["paper-1"]
    assert artifact["artifact_sha256"] == check._reconciliation_checksum(artifact)


def test_rejects_non_paper_broker_before_connection(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FakeBroker(paper=False)

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env=_env(),
        broker_factory=lambda: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "did not report paper mode before connection" in out
    assert fake.connected is False
    assert fake.disconnected is False
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "FAILED"
    assert artifact["broker_responses"] == []
    assert "did not report paper mode before connection" in artifact["error"]


def test_rejects_broker_that_stops_reporting_paper_after_connection(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FlipsAfterConnectBroker()

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env=_env(),
        broker_factory=lambda: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "did not report paper mode after connection" in out
    assert fake.connected is True
    assert fake.disconnected is True


def test_rejects_adapter_with_live_port_metadata(tmp_path, capsys):
    blotter_path = _write_blotter(tmp_path)
    output_path = tmp_path / "paper_reconciliation.json"
    fake = FakeBroker(port=7496)

    result = check.run(
        [
            "--blotter",
            str(blotter_path),
            "--confirm",
            "YES",
            "--reviewed-blotter-sha256",
            stage._file_sha256(blotter_path),
            "--output",
            str(output_path),
        ],
        env=_env(),
        broker_factory=lambda: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Broker adapter port must be 7497" in out
    assert fake.connected is False


# ── Freshness checks (BUG-051) ────────────────────────────────────────────────

def test_freshness_check_rejects_stale_blotter(tmp_path, capsys):
    """Stale blotter must be rejected AND orders still displayed for operator inspection."""
    blotter_path = _write_blotter(tmp_path, generated_at_utc="2020-01-01T12:00:00+00:00")
    result = check.run(
        ["--blotter", str(blotter_path)],
        env=_env(),
        broker_factory=lambda: FakeBroker(),
    )
    out = capsys.readouterr().out
    assert result == 1
    assert "calendar day" in out
    # Orders must be displayed BEFORE freshness fails so operator can inspect
    assert "AAPL" in out


def test_freshness_check_accepts_fresh_blotter(tmp_path, capsys):
    """Blotter generated today must pass the freshness check."""
    blotter_path = _write_blotter(tmp_path)  # defaults to datetime.now(UTC)
    result = check.run(
        ["--blotter", str(blotter_path)],
        env=_env(),
        broker_factory=lambda: FakeBroker(),
    )
    out = capsys.readouterr().out
    assert result == 0
    assert "DRY-RUN OK" in out


def test_freshness_check_custom_max_age(tmp_path, capsys):
    """--max-blotter-age-days=7 should accept a blotter generated 3 days ago."""
    from datetime import timedelta
    three_days_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    blotter_path = _write_blotter(tmp_path, generated_at_utc=three_days_ago)
    result = check.run(
        ["--blotter", str(blotter_path), "--max-blotter-age-days", "7"],
        env=_env(),
        broker_factory=lambda: FakeBroker(),
    )
    assert result == 0


def test_freshness_check_boundary_exactly_one_day_old(tmp_path, capsys):
    """Blotter aged exactly 1 calendar day passes the default max-age-days=1 (strictly greater than rejects)."""
    from datetime import timedelta
    one_day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    blotter_path = _write_blotter(tmp_path, generated_at_utc=one_day_ago)
    result = check.run(
        ["--blotter", str(blotter_path)],
        env=_env(),
        broker_factory=lambda: FakeBroker(),
    )
    assert result == 0


def test_freshness_check_boundary_two_days_old_fails(tmp_path, capsys):
    """Blotter aged 2 calendar days fails the default max-age-days=1."""
    from datetime import timedelta
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    blotter_path = _write_blotter(tmp_path, generated_at_utc=two_days_ago)
    result = check.run(
        ["--blotter", str(blotter_path)],
        env=_env(),
        broker_factory=lambda: FakeBroker(),
    )
    out = capsys.readouterr().out
    assert result == 1
    assert "calendar day" in out
    # Orders still displayed before freshness check fires
    assert "AAPL" in out

"""Tests for durable paper order reconciliation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import paper_order_reconcile_check as check
from scripts import paper_submit_reconcile_check as submit


def _env() -> dict[str, str]:
    return {"PAPER_TRADING": "true", "IBKR_PORT": "7497"}


def _write_step7_reconciliation(tmp_path: Path, *, mutate: Any = None) -> Path:
    path = tmp_path / "paper_submit_reconciliation.json"
    artifact = {
        "schema_version": "paper_submit_reconcile.v1",
        "artifact_type": "paper_submit_reconciliation",
        "run_id": "step-7-run",
        "generated_at_utc": "2026-06-20T18:25:43+00:00",
        "paper_only": True,
        "status": "SUBMITTED",
        "live_port_supported": False,
        "source_blotter_path": "local\\paper_stage_blotter_small.json",
        "source_blotter_run_id": "step-6-run",
        "source_blotter_sha256": "a" * 64,
        "source_blotter_artifact_sha256": "b" * 64,
        "source_candidate_rows_sha256": "c" * 64,
        "order_count": 2,
        "last_attempted_sequence": None,
        "error": None,
        "broker_responses": [
            {
                "sequence": 1,
                "ticker": "APA",
                "direction": "BUY",
                "submitted_quantity": 1.0,
                "limit_price": 33.03,
                "broker_order_id": "3",
                "submitted_at_utc": "2026-06-20T18:25:43+00:00",
                "initial_fill_poll": None,
            },
            {
                "sequence": 2,
                "ticker": "HAL",
                "direction": "BUY",
                "submitted_quantity": 1.0,
                "limit_price": 34.93,
                "broker_order_id": "4",
                "submitted_at_utc": "2026-06-20T18:25:44+00:00",
                "initial_fill_poll": None,
            },
        ],
        "safety": {
            "operator_confirmed_yes": True,
            "paper_env_required": True,
            "ibkr_port": 7497,
            "orders_cancelled": False,
            "circuit_breaker_reset": False,
            "live_orders_allowed": False,
        },
    }
    if mutate is not None:
        mutate(artifact)
    artifact["artifact_sha256"] = submit._reconciliation_checksum(artifact)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    return path


class FakeStatusBroker:
    def __init__(
        self,
        *,
        paper: bool = True,
        port: int = 7497,
        fail_order_id: str | None = None,
        missing_order_id: str | None = None,
        statuses_by_order_id: dict[str, str] | None = None,
    ) -> None:
        self._paper = paper
        self._port = port
        self.fail_order_id = fail_order_id
        self.missing_order_id = missing_order_id
        self.statuses_by_order_id = statuses_by_order_id or {}
        self.connected = False
        self.disconnected = False
        self.queried: list[str] = []
        self.submitted = False
        self.cancelled = False

    @property
    def is_paper(self) -> bool:
        return self._paper

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def get_order_status(self, broker_order_id: str) -> dict[str, Any] | None:
        self.queried.append(broker_order_id)
        if broker_order_id == self.fail_order_id:
            raise RuntimeError("simulated status lookup failure")
        if broker_order_id == self.missing_order_id:
            return None
        broker_status = self.statuses_by_order_id.get(broker_order_id, "Submitted")
        return {
            "broker_order_id": broker_order_id,
            "status": broker_status,
            "filled_quantity": 0.0,
            "remaining_quantity": 1.0,
            "avg_price": 0.0,
            "last_fill_price": 0.0,
            "why_held": "",
        }

    def submit_order(self, _order: object) -> str:
        self.submitted = True
        raise AssertionError("durable reconciliation must not submit")

    def cancel_order(self, _broker_order_id: str) -> bool:
        self.cancelled = True
        raise AssertionError("durable reconciliation must not cancel")


class FlipsAfterConnectBroker(FakeStatusBroker):
    def connect(self) -> None:
        super().connect()
        self._paper = False


def test_reconciles_submitted_orders_and_writes_artifact(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    fake = FakeStatusBroker()

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: fake,
        now_fn=lambda: datetime(2026, 6, 20, 20, 0, tzinfo=UTC),
        run_id_factory=lambda: "order-reconcile-run",
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Paper durable order reconciliation: RECONCILED" in out
    assert fake.connected is True
    assert fake.disconnected is True
    assert fake.queried == ["3", "4"]
    assert fake.submitted is False
    assert fake.cancelled is False

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == "paper_order_reconcile.v1"
    assert artifact["artifact_type"] == "paper_order_reconciliation"
    assert artifact["run_id"] == "order-reconcile-run"
    assert artifact["paper_only"] is True
    assert artifact["status"] == "RECONCILED"
    assert artifact["source_reconciliation_sha256"] == check._file_sha256(source)
    assert artifact["source_reconciliation_artifact_sha256"] == json.loads(
        source.read_text(encoding="utf-8")
    )["artifact_sha256"]
    assert artifact["order_count"] == 2
    assert artifact["status_found_count"] == 2
    assert artifact["query_error_count"] == 0
    assert artifact["clean_broker_status_count"] == 2
    assert artifact["status_issue_count"] == 0
    assert artifact["results"][0]["broker_status_clean"] is True
    assert artifact["results"][0]["status_issue"] is None
    assert artifact["results"][0]["broker_status"]["status"] == "Submitted"
    assert artifact["results"][1]["status_found"] is True
    assert artifact["safety"] == {
        "paper_env_required": True,
        "ibkr_port": 7497,
        "broker_connected_for_reconciliation": True,
        "orders_submitted": False,
        "orders_cancelled": False,
        "circuit_breaker_reset": False,
        "human_yes_consumed": False,
        "prior_artifacts_mutated": False,
        "live_orders_allowed": False,
    }
    assert artifact["artifact_sha256"] == check._artifact_checksum(artifact)


def test_rejects_non_paper_environment_before_broker_connection(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    fake = FakeStatusBroker()

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env={"PAPER_TRADING": "true", "IBKR_PORT": "7496"},
        broker_factory=lambda client_id: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "IBKR_PORT must be exactly 7497" in out
    assert fake.connected is False
    assert not output.exists()


def test_rejects_live_clearance_flag(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env={**_env(), "PAPER_RUN_CLEARED": "true"},
        broker_factory=lambda client_id: FakeStatusBroker(),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "PAPER_RUN_CLEARED=true" in out
    assert not output.exists()


def test_rejects_malformed_source_artifact(tmp_path, capsys):
    source = tmp_path / "paper_submit_reconciliation.json"
    source.write_text("[1, 2, 3]", encoding="utf-8")
    output = tmp_path / "paper_order_reconciliation.json"

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: FakeStatusBroker(),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "must be a JSON object" in out
    assert not output.exists()


def test_rejects_source_checksum_mismatch(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    artifact = json.loads(source.read_text(encoding="utf-8"))
    artifact["broker_responses"][0]["ticker"] = "HPE"
    source.write_text(json.dumps(artifact), encoding="utf-8")
    output = tmp_path / "paper_order_reconciliation.json"

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: FakeStatusBroker(),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "artifact_sha256 mismatch" in out
    assert not output.exists()


def test_rejects_source_without_broker_order_ids(tmp_path, capsys):
    def mutate(artifact: dict[str, Any]) -> None:
        for row in artifact["broker_responses"]:
            row.pop("broker_order_id")

    source = _write_step7_reconciliation(tmp_path, mutate=mutate)
    output = tmp_path / "paper_order_reconciliation.json"

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: FakeStatusBroker(),
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "no broker_order_id values" in out
    assert not output.exists()


def test_rejects_non_paper_broker_before_connection(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    fake = FakeStatusBroker(paper=False)

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "did not report paper mode before connection" in out
    assert fake.connected is False
    assert not output.exists()


def test_rejects_broker_that_stops_reporting_paper_after_connection(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    fake = FlipsAfterConnectBroker()

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "did not report paper mode after connection" in out
    assert fake.connected is True
    assert fake.disconnected is True
    assert not output.exists()


def test_rejects_adapter_with_live_port_metadata(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    fake = FakeStatusBroker(port=7496)

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Broker adapter port must be 7497" in out
    assert fake.connected is False
    assert not output.exists()


def test_broker_query_failure_is_recorded_in_artifact(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    fake = FakeStatusBroker(fail_order_id="4")

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: fake,
        run_id_factory=lambda: "query-failure-run",
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Paper durable order reconciliation: PARTIAL" in out
    assert "Not all broker order IDs were durably reconciled" in out
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["status"] == "PARTIAL"
    assert artifact["query_error_count"] == 1
    assert artifact["results"][1]["error"] == "simulated status lookup failure"


def test_unacceptable_broker_status_writes_partial_artifact_and_fails_closed(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    fake = FakeStatusBroker(statuses_by_order_id={"4": "Cancelled"})

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: fake,
        run_id_factory=lambda: "cancelled-status-run",
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Paper durable order reconciliation: PARTIAL" in out
    assert output.exists()
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["status"] == "PARTIAL"
    assert artifact["status_found_count"] == 2
    assert artifact["clean_broker_status_count"] == 1
    assert artifact["status_issue_count"] == 1
    assert artifact["results"][1]["broker_status_clean"] is False
    assert artifact["results"][1]["status_issue"] == "unacceptable_broker_status:Cancelled"


def test_missing_broker_status_writes_unknown_artifact_and_fails_closed(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    fake = FakeStatusBroker(missing_order_id="4")

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: fake,
        run_id_factory=lambda: "unknown-status-run",
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Paper durable order reconciliation: UNKNOWN" in out
    assert output.exists()
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["status"] == "UNKNOWN"
    assert artifact["status_found_count"] == 1
    assert artifact["results"][1]["status_found"] is False


def test_output_no_clobber(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    output.write_text("existing", encoding="utf-8")
    fake = FakeStatusBroker()

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output)],
        env=_env(),
        broker_factory=lambda client_id: fake,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "already exists" in out
    assert output.read_text(encoding="utf-8") == "existing"
    assert fake.connected is False


def test_overwrite_replaces_existing_output(tmp_path, capsys):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    output.write_text("existing", encoding="utf-8")

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output), "--overwrite"],
        env=_env(),
        broker_factory=lambda client_id: FakeStatusBroker(),
    )

    assert result == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == "paper_order_reconcile.v1"


def test_passes_client_id_to_broker_factory(tmp_path):
    source = _write_step7_reconciliation(tmp_path)
    output = tmp_path / "paper_order_reconciliation.json"
    client_ids: list[int | None] = []

    def broker_factory(client_id: int | None) -> FakeStatusBroker:
        client_ids.append(client_id)
        return FakeStatusBroker()

    result = check.run(
        ["--reconciliation", str(source), "--output", str(output), "--client-id", "17"],
        env=_env(),
        broker_factory=broker_factory,
    )

    assert result == 0
    assert client_ids == [17]

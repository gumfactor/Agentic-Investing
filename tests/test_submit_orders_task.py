"""Tests for the _submit_orders task in daily_paper_trading DAG."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Minimal Airflow env
import os
os.environ.setdefault("AIRFLOW__CORE__UNIT_TEST_MODE", "True")
os.environ.setdefault("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "sqlite:///:memory:")
os.environ.setdefault("PAPER_TRADING", "true")
os.environ.setdefault("IBKR_PORT", "7497")

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_blotter_file(tmp_path: Path, run_id: str = "test-run-1", n_rows: int = 2) -> Path:
    rows = [
        {
            "sequence": i + 1,
            "ticker": f"TICK{i}",
            "direction": "BUY",
            "estimated_shares": 5.0,
            "reference_price": 100.0,
            "review_status": "LOCAL_STAGE_ONLY",
            "current_weight": 0.0,
            "target_weight": 0.02,
            "delta_weight": 0.02,
            "estimated_notional": 500.0,
        }
        for i in range(n_rows)
    ]
    artifact = {
        "schema_version": "paper_stage_blotter.v1",
        "artifact_type": "paper_stage_only_order_blotter",
        "run_id": run_id,
        "paper_only": True,
        "stage_only": True,
        "candidate_rows": rows,
    }
    p = tmp_path / "blotter.json"
    p.write_text(json.dumps(artifact), encoding="utf-8")
    return p


def _make_submit_module(blotter_artifact: dict, broker_responses: list) -> ModuleType:
    """Build a mock scripts.paper_submit_reconcile_check module."""
    mod = ModuleType("scripts.paper_submit_reconcile_check")
    mod.validate_blotter = MagicMock(return_value=blotter_artifact)  # type: ignore[attr-defined]
    mod._validate_api_submittable_quantities = MagicMock(return_value=None)  # type: ignore[attr-defined]
    mod._validate_api_submittable_prices = MagicMock(return_value=None)  # type: ignore[attr-defined]
    mod._submit_orders = MagicMock(return_value=broker_responses)  # type: ignore[attr-defined]
    mod._order_from_row = MagicMock()  # type: ignore[attr-defined]
    mod._file_sha256 = MagicMock(return_value="a" * 64)  # type: ignore[attr-defined]
    mod._build_reconciliation_artifact = MagicMock(  # type: ignore[attr-defined]
        return_value={"schema_version": "paper_submit_reconciliation.v1", "broker_responses": broker_responses}
    )
    return mod


def _make_context(tmp_path: Path, blotter_path: Path, run_id: str = "test-run-1") -> dict:
    ti = MagicMock()
    def xcom_pull(key: str, task_ids: str) -> str | list | None:
        if key == "blotter_path":
            return str(blotter_path)
        if key == "blotter_sha256":
            return "a" * 64
        if key == "selected_order_ids":
            return ["ALL"]
        if key == "approved_by":
            return "operator@example.com"
        return None
    ti.xcom_pull.side_effect = xcom_pull
    return {"ti": ti, "run_id": run_id, "params": {}}


@pytest.fixture(autouse=True)
def paper_env(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("IBKR_PORT", "7497")


class TestSubmitOrdersHappyPath:
    def test_submits_all_orders_and_pushes_xcom(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path)
        artifact = {
            "schema_version": "paper_stage_blotter.v1",
            "run_id": "test-run-1",
            "candidate_rows": [
                {"sequence": 1, "ticker": "AAPL", "direction": "BUY",
                 "estimated_shares": 5.0, "reference_price": 200.0},
                {"sequence": 2, "ticker": "MSFT", "direction": "BUY",
                 "estimated_shares": 3.0, "reference_price": 450.0},
            ],
        }
        broker_responses = [
            {"sequence": 1, "broker_order_id": "ORD-001", "status": "SUBMITTED",
             "initial_fill_poll": None},
            {"sequence": 2, "broker_order_id": "ORD-002", "status": "SUBMITTED",
             "initial_fill_poll": None},
        ]
        ctx = _make_context(tmp_path, blotter_path)
        submit_mod = _make_submit_module(artifact, broker_responses)

        import airflow.dags.daily_paper_trading as dag_mod
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with patch.object(dag_mod, "IBKRBroker", return_value=MagicMock()):
                dag_mod._submit_orders(**ctx)

        ti = ctx["ti"]
        pushed = {call[1]["key"]: call[1]["value"] for call in ti.xcom_push.call_args_list}
        assert "submitted_at_utc" in pushed
        assert pushed["submitted_count"] == 2
        assert pushed["initial_filled_count"] == 0

    def test_counts_initial_fills(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path)
        artifact = {
            "schema_version": "paper_stage_blotter.v1",
            "run_id": "test-run-1",
            "candidate_rows": [
                {"sequence": 1, "ticker": "AAPL", "direction": "BUY",
                 "estimated_shares": 5.0, "reference_price": 200.0},
            ],
        }
        broker_responses = [
            {"sequence": 1, "broker_order_id": "ORD-001", "status": "SUBMITTED",
             "initial_fill_poll": {"status": "Filled", "fill_price": 200.0}},
        ]
        ctx = _make_context(tmp_path, blotter_path)
        submit_mod = _make_submit_module(artifact, broker_responses)

        import airflow.dags.daily_paper_trading as dag_mod
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with patch.object(dag_mod, "IBKRBroker", return_value=MagicMock()):
                dag_mod._submit_orders(**ctx)

        ti = ctx["ti"]
        pushed = {call[1]["key"]: call[1]["value"] for call in ti.xcom_push.call_args_list}
        assert pushed["initial_filled_count"] == 1


class TestSubmitOrdersBlotterHashCheck:
    def test_raises_on_hash_mismatch(self, tmp_path, monkeypatch):
        """_submit_orders raises before any broker call if blotter file changed after approval."""
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path)
        artifact = {
            "candidate_rows": [
                {"sequence": 1, "ticker": "AAPL", "direction": "BUY",
                 "estimated_shares": 5.0, "reference_price": 200.0},
            ],
        }
        submit_mod = _make_submit_module(artifact, [])
        # Return a different hash than what XCom reports as approved
        submit_mod._file_sha256.return_value = "b" * 64  # disk hash differs from approved "a"*64

        ctx = _make_context(tmp_path, blotter_path)  # xcom blotter_sha256 = "a"*64

        import airflow.dags.daily_paper_trading as dag_mod
        from airflow.exceptions import AirflowException
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with pytest.raises(AirflowException, match="modified after operator approval"):
                dag_mod._submit_orders(**ctx)

        # Broker must not have been called
        submit_mod._submit_orders.assert_not_called()

    def test_raises_with_clear_message_when_blotter_sha256_xcom_missing(self, tmp_path, monkeypatch):
        """When blotter_sha256 XCom is None (missing/expired), error is clear — not 'modified'."""
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path)
        artifact = {"candidate_rows": []}
        submit_mod = _make_submit_module(artifact, [])

        ti = MagicMock()
        def xcom_pull(key: str, task_ids: str) -> str | list | None:
            if key == "blotter_path":
                return str(blotter_path)
            if key == "blotter_sha256":
                return None  # XCom missing
            if key == "selected_order_ids":
                return ["ALL"]
            return None
        ti.xcom_pull.side_effect = xcom_pull
        ctx = {"ti": ti, "run_id": "test-sha-none", "params": {}}

        import airflow.dags.daily_paper_trading as dag_mod
        from airflow.exceptions import AirflowException
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with pytest.raises(AirflowException, match="blotter_sha256 XCom not found"):
                dag_mod._submit_orders(**ctx)


class TestSubmitOrdersC1Guard:
    def test_raises_on_none_selected_order_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path)
        artifact = {
            "candidate_rows": [
                {"sequence": 1, "ticker": "AAPL", "direction": "BUY",
                 "estimated_shares": 5.0, "reference_price": 200.0},
            ],
        }
        ti = MagicMock()
        def xcom_pull(key: str, task_ids: str) -> str | list | None:
            if key == "blotter_path":
                return str(blotter_path)
            if key == "selected_order_ids":
                return None  # falsy — must raise
            return "a" * 64
        ti.xcom_pull.side_effect = xcom_pull
        ctx = {"ti": ti, "run_id": "test-run-c1", "params": {}}
        submit_mod = _make_submit_module(artifact, [])

        import airflow.dags.daily_paper_trading as dag_mod
        from airflow.exceptions import AirflowException
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with pytest.raises(AirflowException, match="C1 safety guard"):
                dag_mod._submit_orders(**ctx)

    def test_raises_on_empty_list_selected_order_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path)
        artifact = {
            "candidate_rows": [
                {"sequence": 1, "ticker": "AAPL", "direction": "BUY",
                 "estimated_shares": 5.0, "reference_price": 200.0},
            ],
        }
        ti = MagicMock()
        def xcom_pull(key: str, task_ids: str) -> str | list | None:
            if key == "blotter_path":
                return str(blotter_path)
            if key == "selected_order_ids":
                return []  # empty list — must raise
            return "a" * 64
        ti.xcom_pull.side_effect = xcom_pull
        ctx = {"ti": ti, "run_id": "test-run-c1b", "params": {}}
        submit_mod = _make_submit_module(artifact, [])

        import airflow.dags.daily_paper_trading as dag_mod
        from airflow.exceptions import AirflowException
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with pytest.raises(AirflowException, match="C1 safety guard"):
                dag_mod._submit_orders(**ctx)

    def test_subset_filter_only_submits_approved_sequences(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path, n_rows=3)
        all_rows = [
            {"sequence": i + 1, "ticker": f"T{i}", "direction": "BUY",
             "estimated_shares": 1.0, "reference_price": 100.0}
            for i in range(3)
        ]
        artifact = {"candidate_rows": all_rows}
        broker_responses = [{"sequence": 1, "broker_order_id": "O1", "status": "SUBMITTED",
                              "initial_fill_poll": None}]
        captured_rows: list = []
        def capture_submit(art, broker_fn, **kwargs):
            captured_rows.extend(art["candidate_rows"])
            return broker_responses
        submit_mod = _make_submit_module(artifact, broker_responses)
        submit_mod._submit_orders.side_effect = capture_submit

        ti = MagicMock()
        def xcom_pull(key: str, task_ids: str) -> str | list | None:
            if key == "blotter_path":
                return str(blotter_path)
            if key == "selected_order_ids":
                return [1, 3]  # only sequences 1 and 3
            return "a" * 64
        ti.xcom_pull.side_effect = xcom_pull
        ctx = {"ti": ti, "run_id": "test-run-subset", "params": {}}

        import airflow.dags.daily_paper_trading as dag_mod
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with patch.object(dag_mod, "IBKRBroker", return_value=MagicMock()):
                dag_mod._submit_orders(**ctx)

        submitted_seqs = [r["sequence"] for r in captured_rows]
        assert submitted_seqs == [1, 3]


class TestSubmitOrdersPartialRetry:
    def test_skips_already_submitted_sequences(self, tmp_path, monkeypatch):
        """On retry, sequences with broker_order_id in the partial artifact are skipped."""
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path, n_rows=2)
        all_rows = [
            {"sequence": 1, "ticker": "AAPL", "direction": "BUY",
             "estimated_shares": 5.0, "reference_price": 200.0},
            {"sequence": 2, "ticker": "MSFT", "direction": "BUY",
             "estimated_shares": 3.0, "reference_price": 450.0},
        ]
        artifact = {"candidate_rows": all_rows}

        # Sequence 1 was already submitted in a prior attempt
        run_dir = tmp_path / "test-run-retry"
        run_dir.mkdir()
        partial = {
            "broker_responses": [
                {"sequence": 1, "broker_order_id": "ORD-001-PRIOR", "status": "SUBMITTED",
                 "initial_fill_poll": None}
            ]
        }
        (run_dir / "submit_reconciliation.json").write_text(
            json.dumps(partial), encoding="utf-8"
        )

        captured_rows: list = []
        def capture_submit(art, broker_fn, **kwargs):
            captured_rows.extend(art["candidate_rows"])
            return [{"sequence": 2, "broker_order_id": "ORD-002", "status": "SUBMITTED",
                     "initial_fill_poll": None}]
        submit_mod = _make_submit_module(artifact, [])
        submit_mod._submit_orders.side_effect = capture_submit

        ctx = _make_context(tmp_path, blotter_path, run_id="test-run-retry")

        import airflow.dags.daily_paper_trading as dag_mod
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with patch.object(dag_mod, "IBKRBroker", return_value=MagicMock()):
                dag_mod._submit_orders(**ctx)

        # Only sequence 2 was submitted (sequence 1 was skipped)
        assert [r["sequence"] for r in captured_rows] == [2]

    def test_all_already_submitted_returns_early_without_broker_call(self, tmp_path, monkeypatch):
        """If all sequences are in the partial artifact, return early without calling broker."""
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path, n_rows=1)
        artifact = {
            "candidate_rows": [
                {"sequence": 1, "ticker": "AAPL", "direction": "BUY",
                 "estimated_shares": 5.0, "reference_price": 200.0},
            ]
        }
        run_dir = tmp_path / "test-run-all-done"
        run_dir.mkdir()
        partial = {
            "broker_responses": [
                {"sequence": 1, "broker_order_id": "ORD-001-PRIOR", "status": "SUBMITTED",
                 "initial_fill_poll": None}
            ]
        }
        (run_dir / "submit_reconciliation.json").write_text(
            json.dumps(partial), encoding="utf-8"
        )

        submit_mod = _make_submit_module(artifact, [])
        ctx = _make_context(tmp_path, blotter_path, run_id="test-run-all-done")

        import airflow.dags.daily_paper_trading as dag_mod
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with patch.object(dag_mod, "IBKRBroker", return_value=MagicMock()):
                dag_mod._submit_orders(**ctx)

        # _submit_orders (the script function) should NOT be called
        submit_mod._submit_orders.assert_not_called()

        ti = ctx["ti"]
        pushed = {call[1]["key"]: call[1]["value"] for call in ti.xcom_push.call_args_list}
        assert pushed["submitted_count"] == 1  # from the partial artifact
        # submitted_at_utc must be pushed so wait_for_fills can anchor the fill window
        assert "submitted_at_utc" in pushed
        assert pushed["submitted_at_utc"]  # non-empty timestamp string

    def test_fill_count_recovered_from_existing_artifact_not_hardcoded_zero(self, tmp_path, monkeypatch):
        """On idempotent retry, initial_filled_count reflects actual fills in the partial artifact."""
        monkeypatch.setenv("RQIS_PAPER_ARTIFACT_DIR", str(tmp_path))
        blotter_path = _make_blotter_file(tmp_path, n_rows=2)
        artifact = {
            "candidate_rows": [
                {"sequence": 1, "ticker": "AAPL", "direction": "BUY",
                 "estimated_shares": 5.0, "reference_price": 200.0},
                {"sequence": 2, "ticker": "MSFT", "direction": "BUY",
                 "estimated_shares": 3.0, "reference_price": 450.0},
            ]
        }
        run_dir = tmp_path / "test-run-fill-count"
        run_dir.mkdir()
        partial = {
            "generated_at_utc": "2026-06-25T10:00:00+00:00",
            "broker_responses": [
                {"sequence": 1, "broker_order_id": "O1", "status": "SUBMITTED",
                 "initial_fill_poll": {"status": "Filled", "fill_price": 200.0}},
                {"sequence": 2, "broker_order_id": "O2", "status": "SUBMITTED",
                 "initial_fill_poll": {"status": "Submitted"}},
            ],
        }
        (run_dir / "submit_reconciliation.json").write_text(json.dumps(partial), encoding="utf-8")

        submit_mod = _make_submit_module(artifact, [])
        ctx = _make_context(tmp_path, blotter_path, run_id="test-run-fill-count")

        import airflow.dags.daily_paper_trading as dag_mod
        with patch.dict(sys.modules, {"scripts.paper_submit_reconcile_check": submit_mod}):
            with patch.object(dag_mod, "IBKRBroker", return_value=MagicMock()):
                dag_mod._submit_orders(**ctx)

        ti = ctx["ti"]
        pushed = {call[1]["key"]: call[1]["value"] for call in ti.xcom_push.call_args_list}
        assert pushed["submitted_count"] == 2
        assert pushed["initial_filled_count"] == 1  # only sequence 1 was Filled
        assert pushed["submitted_at_utc"] == "2026-06-25T10:00:00+00:00"  # from artifact

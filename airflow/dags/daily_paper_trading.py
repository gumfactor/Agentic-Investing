"""Daily paper-trading pipeline (M5.4).

Runs the full automated paper-trading workflow: data-freshness preflight →
target-weight construction → live IBKR position snapshot → order candidates →
risk/compliance gates → blotter artifact → optional IBKR what-if validation →
operator approval gate (C1) → IBKR paper submission → durable fill
reconciliation → audit ledger.

Schedule: 23:00 ET weekdays, after the signal pipeline (21:30 ET) has
had time to complete. An ExternalTaskSensor provides an exact trigger; the
90-minute schedule gap is the fallback buffer.

Approval gate: the BlotterApprovalSensor polls blotter_approvals until an
operator inserts a row via the paper_approve_blotter CLI (interim) or the
Streamlit dashboard (M5.8). SHA-256 tamper-detection prevents submission of
a modified blotter.

Safety rules enforced:
  C1 — no submission without operator approval row in blotter_approvals
  C4 — circuit breaker OPEN fails the pipeline; never reset automatically
  C5 — no secrets in code; all credentials from environment
  C9 — PAPER_RUN_CLEARED=true is rejected at every gate

DAG structure:
  wait_for_signal_pipeline
      └── verify_inputs
              └── construct_target
                      └── fetch_ibkr_snapshot
                              └── gen_candidates
                                      └── risk_compliance_gate
                                              └── build_blotter
                                                      └── whatif_validate  (optional)
                                                              └── wait_approval  (C1 sensor)
                                                                      └── submit_orders
                                                                              └── wait_for_fills
                                                                                      └── durable_reconcile
                                                                                              └── write_ledger

Artifact storage:
  All artifacts are written to a per-run local directory:
      {RQIS_PAPER_ARTIFACT_DIR}/{safe_dag_run_id}/
  Default RQIS_PAPER_ARTIFACT_DIR: /opt/airflow/rqis_paper (shared Docker
  Compose volume). Each task pushes its artifact path to XCom.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pendulum
from airflow import DAG
from airflow.exceptions import AirflowException
from airflow.operators.python import PythonOperator
from airflow.sensors.external_task import ExternalTaskSensor
from airflow.sensors.time_delta import TimeDeltaSensor
from sqlalchemy import create_engine, text

try:
    from execution.brokers.ibkr import IBKRBroker
except ImportError:
    IBKRBroker = None  # type: ignore[assignment,misc]

_MARKET_TZ = pendulum.timezone("America/New_York")
_DAG_START_DATE = pendulum.datetime(2026, 7, 1, 23, 0, tz=_MARKET_TZ)
_DEFAULT_STRATEGY_ID = "v1_base_momentum"
_DEFAULT_STRATEGY_CONFIG = "config/strategy/v1_base_momentum.yaml"
_DEFAULT_MIN_DELTA_WEIGHT = 0.005
_DEFAULT_WHATIF_ENABLED = True
_DEFAULT_APPROVAL_TIMEOUT_HOURS = 8.0

# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_run_id(run_id: str) -> str:
    """Sanitise Airflow run_id for use as a filesystem directory name."""
    return re.sub(r"[^\w\-]", "_", run_id)[:200]


def _artifact_dir(run_id: str) -> Path:
    base = Path(os.environ.get("RQIS_PAPER_ARTIFACT_DIR", "/opt/airflow/rqis_paper"))
    d = base / _safe_run_id(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _require_paper_env(env: dict[str, str]) -> None:
    """Fail fast if not in paper-trading mode."""
    if env.get("PAPER_TRADING", "").strip().lower() != "true":
        raise AirflowException("PAPER_TRADING must be 'true' for the paper-trading pipeline.")
    if env.get("IBKR_PORT", "").strip() != "7497":
        raise AirflowException("IBKR_PORT must be '7497' for the paper-trading pipeline.")
    if env.get("PAPER_RUN_CLEARED", "false").strip().lower() == "true":
        raise AirflowException(
            "PAPER_RUN_CLEARED=true is a live-capital clearance flag; "
            "it must be unset for the paper-trading pipeline."
        )


def _alert_operator(context: Any) -> None:
    """DAG-level failure callback — extend to send Slack/email via .env config."""
    import structlog

    log = structlog.get_logger("rqis.airflow")
    ti = context.get("task_instance")
    log.error(
        "paper_trading_task_failed",
        dag_id=context.get("dag").dag_id if context.get("dag") else "unknown",
        task_id=ti.task_id if ti else "unknown",
        run_id=context.get("run_id"),
        exception=str(context.get("exception", "")),
    )


def _persist_snapshot_to_db(
    *,
    database_url: str,
    snapshot_date: str,
    strategy_id: str,
    dag_run_id: str,
    cash_usd: float,
    positions: list[dict[str, Any]],
    nav_usd: float,
) -> None:
    """Upsert the nightly IBKR snapshot into portfolio_snapshots for dashboard reads.

    Uses ON CONFLICT upsert so a DAG retry on the same trading date overwrites
    the prior row rather than raising a unique-constraint violation. Called after
    the JSON artifact is written; callers must catch and log exceptions so a DB
    failure does not block the rest of the pipeline.
    """
    import uuid as _uuid
    from datetime import UTC, datetime as _datetime

    row_id = str(_uuid.uuid4())
    fetched_at = _datetime.now(UTC).isoformat()

    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO portfolio_snapshots
                        (id, snapshot_date, strategy_id, dag_run_id, fetched_at_utc,
                         cash_usd, positions, nav_usd, source)
                    VALUES
                        (:id, :snapshot_date, :strategy_id, :dag_run_id, :fetched_at_utc,
                         :cash_usd, :positions, :nav_usd, 'ibkr_paper')
                    ON CONFLICT (snapshot_date, strategy_id)
                    DO UPDATE SET
                        cash_usd      = EXCLUDED.cash_usd,
                        positions     = EXCLUDED.positions,
                        nav_usd       = EXCLUDED.nav_usd,
                        fetched_at_utc = EXCLUDED.fetched_at_utc,
                        dag_run_id    = EXCLUDED.dag_run_id
                """),
                {
                    "id": row_id,
                    "snapshot_date": snapshot_date,
                    "strategy_id": strategy_id,
                    "dag_run_id": dag_run_id,
                    "fetched_at_utc": fetched_at,
                    "cash_usd": cash_usd,
                    "positions": json.dumps(positions),
                    "nav_usd": nav_usd,
                },
            )
    finally:
        engine.dispose()


# ── Default task args (defined after _alert_operator to allow callable ref) ───

_default_args: dict[str, Any] = {
    "owner": "rqis",
    "depends_on_past": False,
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "on_failure_callback": _alert_operator,
}


# ── Task: verify_inputs (Step 2) ───────────────────────────────────────────────

def _verify_inputs(**context: Any) -> None:
    """Validate that the DB has fresh prices and alpha scores for today's run."""
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))

    params = context["params"]
    strategy_id = params.get("strategy_id", _DEFAULT_STRATEGY_ID)
    strategy_config = params.get("strategy_config", _DEFAULT_STRATEGY_CONFIG)

    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))
    from scripts.paper_inputs_check import run as inputs_run

    rc = inputs_run(
        argv=[
            "--strategy-id", strategy_id,
            "--strategy-config", strategy_config,
        ]
    )
    if rc != 0:
        raise AirflowException(
            f"verify_inputs (Step 2) failed for strategy_id={strategy_id!r}. "
            "Check the task log for FAIL lines."
        )

    context["ti"].xcom_push(key="strategy_id", value=strategy_id)
    context["ti"].xcom_push(key="strategy_config_path", value=strategy_config)


# ── Task: construct_target (Step 3) ───────────────────────────────────────────

def _construct_target(**context: Any) -> None:
    """Build equal-weight target portfolio weights from latest alpha scores."""
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    from scripts.paper_inputs_check import CheckRecorder, load_strategy_config
    from scripts.paper_target_check import construct_target_portfolio

    ti = context["ti"]
    strategy_id: str = ti.xcom_pull(key="strategy_id", task_ids="verify_inputs")
    strategy_config_path_str: str = ti.xcom_pull(
        key="strategy_config_path", task_ids="verify_inputs"
    )
    strategy_config_path = _Path(strategy_config_path_str)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise AirflowException("DATABASE_URL not set.")

    strategy_config = load_strategy_config(strategy_config_path)
    engine = create_engine(database_url)
    recorder = CheckRecorder()

    target = construct_target_portfolio(
        engine=engine,
        strategy_config_path=strategy_config_path,
        strategy_config=strategy_config,
        strategy_id=strategy_id,
        max_price_age_days=7,
        max_score_age_days=7,
        min_overlap=None,
        today=date.today(),
        recorder=recorder,
    )
    engine.dispose()

    if target is None or not recorder.is_ok:
        issues = "; ".join(recorder.issues)
        raise AirflowException(f"construct_target (Step 3) failed: {issues}")

    weights = {pos.ticker: pos.target_weight for pos in target.positions}
    ti.xcom_push(key="target_weights_json", value=json.dumps(weights))
    ti.xcom_push(key="score_date", value=str(target.as_of_date))
    ti.xcom_push(key="target_method", value=target.method)


# ── Task: fetch_ibkr_snapshot (NEW) ───────────────────────────────────────────

def _fetch_ibkr_snapshot(**context: Any) -> None:
    """Connect to IBKR paper account and read current positions, cash, and NAV.

    Replaces the operator-maintained local/paper_portfolio_snapshot.json.
    Writes the snapshot to a per-run artifact directory and pushes the path
    to XCom for downstream tasks. Also persists the snapshot to the
    portfolio_snapshots DB table for the Streamlit dashboard; a DB failure
    is logged but does not abort the pipeline (the JSON artifact is durable).
    """
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    ti = context["ti"]
    run_id: str = context["run_id"]

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise AirflowException("DATABASE_URL not set.")

    strategy_id: str = (
        ti.xcom_pull(key="strategy_id", task_ids="verify_inputs")
        or _DEFAULT_STRATEGY_ID
    )

    trading_date = date.today()

    broker = IBKRBroker()
    if not broker.is_paper:
        raise AirflowException("IBKRBroker is not in paper mode.")

    broker.connect()
    try:
        if not broker.is_paper:
            raise AirflowException("Broker switched to non-paper mode after connect.")

        positions_raw: dict[str, float] = broker.get_positions()
        cash_usd: float = broker.get_cash_balance_usd()
        nav_usd: float = broker.get_account_value()
    finally:
        broker.disconnect()

    if nav_usd <= 0 or not (nav_usd == nav_usd):  # NaN check
        raise AirflowException(f"IBKR paper NAV is not a finite positive number: {nav_usd}")

    # Fetch latest close prices for held positions from daily_prices.
    # These EOD prices become the limit-order reference prices for order candidates.
    # This is intentional: the snapshot is taken at 23:00 ET after market close,
    # so the most recent daily_prices row IS today's close. Stale prices (older
    # than 3 calendar days) raise an error rather than submitting at a bad price.
    _MAX_PRICE_AGE_DAYS = 3
    position_list: list[dict[str, Any]] = []
    price_close_date: str | None = None
    if positions_raw:
        tickers = list(positions_raw.keys())
        engine = create_engine(database_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT DISTINCT ON (ticker) ticker, close::float AS close, "
                        "date::text AS price_date "
                        "FROM daily_prices "
                        "WHERE ticker = ANY(:tickers) "
                        "ORDER BY ticker, date DESC"
                    ),
                    {"tickers": tickers},
                ).fetchall()
        finally:
            engine.dispose()

        price_map: dict[str, tuple[float, str]] = {
            r.ticker: (float(r.close), r.price_date) for r in rows
        }
        for ticker, qty in positions_raw.items():
            entry = price_map.get(ticker)
            if entry is None:
                raise AirflowException(
                    f"No price found in daily_prices for held position {ticker!r}. "
                    "Refresh daily data before running the paper pipeline."
                )
            price, close_date_str = entry
            # Track the oldest price date across all positions
            if price_close_date is None or close_date_str < price_close_date:
                price_close_date = close_date_str
            position_list.append({
                "ticker": ticker,
                "quantity": qty,
                "price": price,
                "price_date": close_date_str,
            })

        # Fail if the reference prices are too stale to use as limit price anchors
        if price_close_date is not None:
            from datetime import date as _date
            price_age = (trading_date - _date.fromisoformat(price_close_date)).days
            if price_age > _MAX_PRICE_AGE_DAYS:
                raise AirflowException(
                    f"Position reference prices are {price_age} calendar days old "
                    f"(oldest close date: {price_close_date}). "
                    f"Prices older than {_MAX_PRICE_AGE_DAYS} days are too stale "
                    "to use as limit-order price anchors. Refresh daily_prices first."
                )

    snapshot = {
        "schema_version": "paper_portfolio_snapshot.v1",
        "as_of": str(trading_date),
        "price_close_date": price_close_date,
        "cash": round(cash_usd, 2),
        "nav_usd": round(nav_usd, 2),
        "positions": position_list,
    }

    artifact_path = _artifact_dir(run_id) / "portfolio_snapshot.json"
    artifact_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")

    # Persist to DB so the Streamlit dashboard can read current portfolio state.
    # Non-blocking: the JSON artifact above is the durable record; the DB write
    # is a secondary read path for the dashboard and must not abort the pipeline.
    #
    # The DB payload uses dashboard field names (current_price) rather than the
    # pipeline artifact field names (price/price_date).  The artifact is left
    # unchanged so downstream pipeline tasks are unaffected.
    _db_positions = [
        {"ticker": p["ticker"], "quantity": p["quantity"], "current_price": p["price"]}
        for p in position_list
    ]
    try:
        _persist_snapshot_to_db(
            database_url=database_url,
            snapshot_date=str(trading_date),
            strategy_id=strategy_id,
            dag_run_id=run_id,
            cash_usd=round(cash_usd, 2),
            positions=_db_positions,
            nav_usd=round(nav_usd, 2),
        )
    except Exception as _exc:
        import structlog as _sl
        _sl.get_logger("rqis.airflow").warning(
            "portfolio_snapshot_db_persist_failed",
            error=str(_exc),
            trading_date=str(trading_date),
            strategy_id=strategy_id,
        )

    ti.xcom_push(key="snapshot_path", value=str(artifact_path))
    ti.xcom_push(key="trading_date", value=str(trading_date))


# ── Task: gen_candidates (Step 4) ─────────────────────────────────────────────

def _gen_candidates(**context: Any) -> None:
    """Generate SELL-before-BUY order candidates from weight deltas."""
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    from scripts.paper_order_candidates_check import run as candidates_run

    ti = context["ti"]
    strategy_id: str = ti.xcom_pull(key="strategy_id", task_ids="verify_inputs")
    strategy_config_path: str = ti.xcom_pull(
        key="strategy_config_path", task_ids="verify_inputs"
    )
    snapshot_path: str = ti.xcom_pull(key="snapshot_path", task_ids="fetch_ibkr_snapshot")
    trading_date: str = ti.xcom_pull(key="trading_date", task_ids="fetch_ibkr_snapshot")
    min_delta = context["params"].get("min_delta_weight", _DEFAULT_MIN_DELTA_WEIGHT)

    # Write candidates output to the artifact dir for downstream inspection
    run_id: str = context["run_id"]
    candidates_out = _artifact_dir(run_id) / "candidates.json"

    rc = candidates_run(
        argv=[
            "--strategy-id", strategy_id,
            "--strategy-config", strategy_config_path,
            "--portfolio-input", snapshot_path,
            "--min-delta-weight", str(min_delta),
        ]
    )
    if rc != 0:
        raise AirflowException(
            "gen_candidates (Step 4) failed. Check the task log for FAIL lines."
        )

    # Re-run the candidate logic directly to get the structured data for XCom.
    from scripts.paper_inputs_check import CheckRecorder, load_strategy_config
    from scripts.paper_order_candidates_check import build_order_candidates, load_portfolio_snapshot
    from scripts.paper_target_check import construct_target_portfolio

    database_url = os.environ["DATABASE_URL"]
    engine = create_engine(database_url)
    recorder = CheckRecorder()
    strategy_config = load_strategy_config(_Path(strategy_config_path))
    snapshot = load_portfolio_snapshot(_Path(snapshot_path))

    target = construct_target_portfolio(
        engine=engine,
        strategy_config_path=_Path(strategy_config_path),
        strategy_config=strategy_config,
        strategy_id=strategy_id,
        max_price_age_days=7,
        max_score_age_days=7,
        min_overlap=None,
        today=date.fromisoformat(trading_date),
        recorder=recorder,
    )
    engine.dispose()

    if target is None or not recorder.is_ok:
        raise AirflowException("gen_candidates: target construction failed during data extraction")

    candidates = build_order_candidates(
        target=target,
        snapshot=snapshot,
        min_delta_weight=float(min_delta),
    )

    candidates_data = [
        {
            "ticker": c.ticker,
            "direction": c.direction,
            "current_weight": c.current_weight,
            "target_weight": c.target_weight,
            "delta_weight": c.delta_weight,
            "reference_price": c.reference_price,
            "estimated_shares": c.estimated_shares,
            "estimated_notional": c.estimated_notional,
        }
        for c in candidates
    ]
    candidates_out.write_text(
        json.dumps(candidates_data, indent=2) + "\n", encoding="utf-8"
    )

    ti.xcom_push(key="candidates_path", value=str(candidates_out))
    ti.xcom_push(key="candidate_count", value=len(candidates_data))


# ── Task: risk_compliance_gate (Step 5) ───────────────────────────────────────

def _risk_compliance_gate(**context: Any) -> None:
    """Run hard-limit risk and compliance gates against order candidates."""
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    from scripts.paper_risk_compliance_check import run as risk_run

    ti = context["ti"]
    strategy_id: str = ti.xcom_pull(key="strategy_id", task_ids="verify_inputs")
    strategy_config_path: str = ti.xcom_pull(
        key="strategy_config_path", task_ids="verify_inputs"
    )
    snapshot_path: str = ti.xcom_pull(key="snapshot_path", task_ids="fetch_ibkr_snapshot")

    rc = risk_run(
        argv=[
            "--strategy-id", strategy_id,
            "--strategy-config", strategy_config_path,
            "--portfolio-input", snapshot_path,
        ]
    )
    if rc != 0:
        raise AirflowException(
            "risk_compliance_gate (Step 5) failed. "
            "Check the task log for FAIL lines (gate violations, circuit breaker, etc.)."
        )

    ti.xcom_push(key="gate_passed", value=True)


# ── Task: build_blotter (Step 6) ──────────────────────────────────────────────

def _build_blotter(**context: Any) -> None:
    """Build the stage-only blotter artifact and notify the operator for review."""
    import hashlib
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    from scripts.paper_stage_blotter_check import run as blotter_run

    ti = context["ti"]
    strategy_id: str = ti.xcom_pull(key="strategy_id", task_ids="verify_inputs")
    strategy_config_path: str = ti.xcom_pull(
        key="strategy_config_path", task_ids="verify_inputs"
    )
    snapshot_path: str = ti.xcom_pull(key="snapshot_path", task_ids="fetch_ibkr_snapshot")
    run_id: str = context["run_id"]

    blotter_path = _artifact_dir(run_id) / "blotter.json"

    rc = blotter_run(
        argv=[
            "--strategy-id", strategy_id,
            "--strategy-config", strategy_config_path,
            "--portfolio-input", snapshot_path,
            "--output", str(blotter_path),
        ]
    )
    if rc != 0:
        raise AirflowException(
            "build_blotter (Step 6) failed. Check the task log for FAIL lines."
        )

    artifact = json.loads(blotter_path.read_text(encoding="utf-8"))
    blotter_run_id: str = artifact["run_id"]

    sha256 = hashlib.sha256(blotter_path.read_bytes()).hexdigest()

    ti.xcom_push(key="blotter_path", value=str(blotter_path))
    ti.xcom_push(key="blotter_run_id", value=blotter_run_id)
    ti.xcom_push(key="blotter_sha256", value=sha256)
    ti.xcom_push(key="candidate_count", value=len(artifact.get("candidate_rows", [])))

    _notify_operator_blotter_ready(
        blotter_run_id=blotter_run_id,
        blotter_path=blotter_path,
        candidate_count=len(artifact.get("candidate_rows", [])),
        strategy_id=strategy_id,
        trading_date=artifact.get("trading_date", ""),
        sha256=sha256,
    )


def _notify_operator_blotter_ready(
    *,
    blotter_run_id: str,
    blotter_path: Path,
    candidate_count: int,
    strategy_id: str,
    trading_date: str,
    sha256: str,
) -> None:
    """Log blotter-ready notification; extend to Slack/email via .env config."""
    import structlog

    log = structlog.get_logger("rqis.airflow")
    log.info(
        "blotter_ready_for_review",
        blotter_run_id=blotter_run_id,
        blotter_path=str(blotter_path),
        candidate_count=candidate_count,
        strategy_id=strategy_id,
        trading_date=trading_date,
        sha256=sha256,
        instructions=(
            f"Review and approve: "
            f"python -m scripts.paper_approve_blotter --blotter {blotter_path}"
        ),
    )


# ── Task: whatif_validate (Step 7.5, optional) ────────────────────────────────

def _whatif_validate(**context: Any) -> None:
    """Run IBKR what-if validation on all blotter candidates (non-transmitting)."""
    import sys
    from pathlib import Path as _Path

    whatif_enabled = context["params"].get("whatif_enabled", _DEFAULT_WHATIF_ENABLED)
    if not whatif_enabled:
        context["ti"].xcom_push(key="whatif_path", value=None)
        return

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    from scripts.paper_whatif_check import run as whatif_run

    ti = context["ti"]
    blotter_path: str = ti.xcom_pull(key="blotter_path", task_ids="build_blotter")
    run_id: str = context["run_id"]
    whatif_out = _artifact_dir(run_id) / "whatif.json"

    rc = whatif_run(
        argv=[
            "--blotter", blotter_path,
            "--output", str(whatif_out),
        ]
    )
    if rc != 0:
        raise AirflowException(
            "whatif_validate (Step 7.5) failed. "
            "IBKR rejected one or more orders in what-if mode; see task log."
        )

    ti.xcom_push(key="whatif_path", value=str(whatif_out))


# ── Task: submit_orders (Step 7) ──────────────────────────────────────────────

def _submit_orders(**context: Any) -> None:
    """Submit approved blotter orders to IBKR paper (port 7497).

    Reads the approval record from XCom (set by BlotterApprovalSensor), filters
    blotter candidates to the approved subset, and submits.

    On retry (after a partial connection failure), reads the partial
    reconciliation artifact to skip already-submitted orders.
    """
    import copy
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    from scripts.paper_submit_reconcile_check import (
        _build_reconciliation_artifact,
        _order_from_row,
        _submit_orders as _do_submit,
        _validate_api_submittable_prices,
        _validate_api_submittable_quantities,
        validate_blotter,
    )

    ti = context["ti"]
    blotter_path_str: str = ti.xcom_pull(key="blotter_path", task_ids="build_blotter")
    blotter_sha256: str = ti.xcom_pull(key="blotter_sha256", task_ids="build_blotter")
    selected_order_ids = ti.xcom_pull(key="selected_order_ids", task_ids="wait_approval")
    approved_by: str = ti.xcom_pull(key="approved_by", task_ids="wait_approval")
    quantity_overrides = ti.xcom_pull(key="quantity_overrides", task_ids="wait_approval") or {}

    blotter_path = _Path(blotter_path_str)

    # C1: read the file ONCE so the hash and the submitted rows come from the same
    # bytes, closing the TOCTOU window where a swapped-then-restored file could pass
    # validation but have its hash checked against the original approved content.
    import hashlib as _hashlib
    if blotter_sha256 is None:
        raise AirflowException(
            "blotter_sha256 XCom not found — build_blotter task may not have completed "
            "or the XCom has expired. Cannot verify blotter integrity before submission."
        )
    raw_bytes = blotter_path.read_bytes()
    current_sha = _hashlib.sha256(raw_bytes).hexdigest()
    if current_sha != blotter_sha256:
        raise AirflowException(
            f"Blotter artifact was modified after operator approval. "
            f"Approved SHA-256: {blotter_sha256!r}, current on disk: {current_sha!r}. "
            "This is a C1 safety violation — do not retry without a fresh approval."
        )
    # Parse the artifact from the same bytes the hash was computed over.
    # validate_blotter re-reads the path for its internal consistency checks
    # (schema version, candidate_rows_sha256, artifact_sha256, provenance), but
    # the rows used for submission come from raw_bytes, not that second read.
    artifact = json.loads(raw_bytes.decode("utf-8"))
    validate_blotter(blotter_path)

    # Filter to approved candidates only (C1: per-order selection is mandatory)
    all_rows = artifact["candidate_rows"]
    if isinstance(selected_order_ids, str):
        selected_order_ids = json.loads(selected_order_ids)

    if not selected_order_ids and selected_order_ids != ["ALL"]:
        raise AirflowException(
            "selected_order_ids from wait_approval XCom is empty or None. "
            "The approval sensor must push a non-empty list or [\"ALL\"]. "
            "This is a C1 safety guard — no orders will be submitted."
        )

    if selected_order_ids == ["ALL"]:
        rows_to_submit = all_rows
    else:
        approved_seqs = set(int(x) for x in selected_order_ids)
        rows_to_submit = [r for r in all_rows if int(r["sequence"]) in approved_seqs]

    if not rows_to_submit:
        raise AirflowException(
            "No candidate rows remain after filtering by selected_order_ids. "
            "Verify that the approval selected at least one order."
        )

    if quantity_overrides:
        # BUG-005: validate each override server-side before applying it.
        # The dashboard caps overrides before writing to the DB, but the Airflow
        # process must re-validate so a tampered or erroneous DB row cannot cause
        # an oversized order to bypass the dashboard cap.
        import math as _math
        for seq_str, override_qty in quantity_overrides.items():
            seq_int = int(seq_str)
            original_row = next(
                (r for r in rows_to_submit if int(r["sequence"]) == seq_int), None
            )
            if original_row is None:
                raise AirflowException(
                    f"quantity_overrides references sequence {seq_str!r} which is not in "
                    "the approved rows. This may indicate a tampered approval record."
                )
            original_qty = float(original_row.get("quantity", original_row.get("estimated_shares", 0)))
            override_qty_f = float(override_qty)
            if (
                not isinstance(override_qty, int)
                or isinstance(override_qty, bool)  # bool is int subclass; reject True/False
                or override_qty_f <= 0
                or not _math.isfinite(override_qty_f)
                or override_qty_f > original_qty + 1e-6
            ):
                raise AirflowException(
                    f"quantity_overrides[{seq_str!r}] = {override_qty!r} is invalid: "
                    f"must be a positive integer ≤ original approved quantity "
                    f"({original_qty:.0f}). Aborting to prevent oversized submission."
                )

        for i, row in enumerate(rows_to_submit):
            override_qty = quantity_overrides.get(str(row["sequence"]))
            if override_qty is not None:
                rows_to_submit[i] = {**row, "quantity": override_qty}

    _validate_api_submittable_quantities(rows_to_submit)
    _validate_api_submittable_prices(rows_to_submit)

    run_id: str = context["run_id"]
    reconciliation_path = _artifact_dir(run_id) / "submit_reconciliation.json"

    # Partial retry detection: if the reconciliation artifact already exists,
    # extract broker_order_ids that were successfully recorded and skip them.
    # BUG-006: fail closed on corrupt artifacts — a partial write or tampering
    # must not allow silent resubmission of already-accepted orders.
    already_submitted_seqs: set[int] = set()
    if reconciliation_path.exists():
        try:
            partial = json.loads(reconciliation_path.read_text(encoding="utf-8"))
            for resp in partial.get("broker_responses", []):
                if resp.get("broker_order_id"):
                    already_submitted_seqs.add(int(resp["sequence"]))
        except Exception as _exc:
            raise AirflowException(
                f"Partial reconciliation artifact at {reconciliation_path} is corrupt or "
                f"unreadable: {_exc}. Cannot safely determine which orders were already "
                "submitted — manual broker reconciliation required before retrying."
            ) from _exc

    if already_submitted_seqs:
        rows_to_submit = [
            r for r in rows_to_submit
            if int(r["sequence"]) not in already_submitted_seqs
        ]

    if not rows_to_submit and not already_submitted_seqs:
        raise AirflowException("All candidates were filtered out; nothing to submit.")

    if not rows_to_submit:
        # All orders were submitted in a prior attempt; load the partial artifact
        # and mark as SUBMITTED.
        from datetime import UTC, datetime as _datetime
        existing = json.loads(reconciliation_path.read_text(encoding="utf-8"))
        existing_responses = existing.get("broker_responses", [])
        # Recover the original submission timestamp so wait_for_fills can anchor
        # its fill window correctly on retry (it raises if submitted_at_utc is missing).
        submitted_at = existing.get("generated_at_utc") or _datetime.now(UTC).isoformat()
        # Re-count actual fills from the existing artifact rather than hardcoding 0.
        existing_filled = sum(
            1 for r in existing_responses
            if r.get("initial_fill_poll") and r["initial_fill_poll"].get("status") == "Filled"
        )
        ti.xcom_push(key="submitted_at_utc", value=submitted_at)
        ti.xcom_push(key="reconciliation_path", value=str(reconciliation_path))
        ti.xcom_push(key="submitted_count", value=len(existing_responses))
        ti.xcom_push(key="initial_filled_count", value=existing_filled)
        return

    filtered_artifact = copy.deepcopy(dict(artifact))
    filtered_artifact["candidate_rows"] = rows_to_submit

    from datetime import UTC, datetime

    sub_run_id = str(uuid.uuid4())
    previous_responses: list[dict[str, Any]] = []
    if reconciliation_path.exists():
        try:
            prev = json.loads(reconciliation_path.read_text(encoding="utf-8"))
            previous_responses = list(prev.get("broker_responses", []))
        except Exception as _exc2:
            # The first read already succeeded (deduplication is safe), but we
            # must not silently drop prior responses from the audit trail.
            raise AirflowException(
                f"Reconciliation artifact at {reconciliation_path} was readable for "
                f"deduplication but failed on second read for audit assembly: {_exc2}. "
                "Manual reconciliation required."
            ) from _exc2

    def _on_progress(
        responses: list[dict[str, Any]],
        status: str,
        seq: int | None,
        error: str | None,
    ) -> None:
        combined = previous_responses + list(responses)
        progress = _build_reconciliation_artifact(
            blotter_path=blotter_path,
            blotter=artifact,
            broker_responses=combined,
            run_id=sub_run_id,
            now=datetime.now(UTC),
            status=status,
            last_attempted_sequence=seq,
            error=error,
        )
        reconciliation_path.write_text(
            json.dumps(progress, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    broker_responses = _do_submit(
        filtered_artifact,
        lambda: IBKRBroker(),
        client_id=None,
        now_fn=lambda: datetime.now(UTC),
        on_progress=_on_progress,
    )

    all_responses = previous_responses + broker_responses
    final_artifact = _build_reconciliation_artifact(
        blotter_path=blotter_path,
        blotter=artifact,
        broker_responses=all_responses,
        run_id=sub_run_id,
        now=datetime.now(UTC),
        status="SUBMITTED",
    )
    reconciliation_path.write_text(
        json.dumps(final_artifact, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    filled = sum(
        1 for r in broker_responses
        if r.get("initial_fill_poll") and r["initial_fill_poll"].get("status") == "Filled"
    )

    from datetime import UTC, datetime as _datetime
    ti.xcom_push(key="submitted_at_utc", value=_datetime.now(UTC).isoformat())
    ti.xcom_push(key="reconciliation_path", value=str(reconciliation_path))
    ti.xcom_push(key="submitted_count", value=len(all_responses))
    ti.xcom_push(key="initial_filled_count", value=filled)


# ── Task: wait_for_fills ──────────────────────────────────────────────────────

def _wait_for_fills(**context: Any) -> None:
    """Sleep until 30 minutes have elapsed since submit_orders completed.

    Uses wall-clock time from the submitted_at_utc XCom pushed by submit_orders,
    not the DAG execution_date, so the wait is anchored to actual submission time.
    """
    import time
    from datetime import UTC, datetime as _datetime, timedelta as _timedelta

    ti = context["ti"]
    submitted_at_str: str | None = ti.xcom_pull(
        key="submitted_at_utc", task_ids="submit_orders"
    )
    if not submitted_at_str:
        raise AirflowException(
            "submitted_at_utc XCom not found — submit_orders may have failed."
        )
    submitted_at = _datetime.fromisoformat(submitted_at_str)
    target = submitted_at + _timedelta(minutes=30)
    remaining = (target - _datetime.now(UTC)).total_seconds()
    if remaining > 0:
        import structlog as _sl
        _sl.get_logger("rqis.airflow").info(
            "wait_for_fills_sleeping", seconds=int(remaining)
        )
        time.sleep(remaining)


# ── Task: durable_reconcile (Step 8) ──────────────────────────────────────────

def _durable_reconcile(**context: Any) -> None:
    """Query IBKR for current fill status of submitted orders."""
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    from scripts.paper_order_reconcile_check import run as reconcile_run

    ti = context["ti"]
    reconciliation_path: str = ti.xcom_pull(
        key="reconciliation_path", task_ids="submit_orders"
    )
    run_id: str = context["run_id"]
    order_reconcile_out = _artifact_dir(run_id) / "order_reconciliation.json"

    rc = reconcile_run(
        argv=[
            "--reconciliation", reconciliation_path,
            "--output", str(order_reconcile_out),
        ]
    )
    # Non-zero exit means UNKNOWN or PARTIAL statuses; capture but don't hard-fail
    # (the write_ledger task records the unresolved state for operator follow-up).
    ti.xcom_push(key="order_reconciliation_path", value=str(order_reconcile_out))
    ti.xcom_push(key="reconcile_clean", value=(rc == 0))


# ── Task: write_ledger (Step 9) ───────────────────────────────────────────────

def _write_ledger(**context: Any) -> None:
    """Write the run audit record and append to the operational ledger."""
    import sys
    from pathlib import Path as _Path

    _require_paper_env(dict(os.environ))
    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    from scripts.paper_run_audit_check import run as audit_run
    from scripts.paper_operational_ledger_check import run as ledger_run

    ti = context["ti"]
    run_id: str = context["run_id"]
    artifact_root = _artifact_dir(run_id)

    blotter_path: str = ti.xcom_pull(key="blotter_path", task_ids="build_blotter")
    reconciliation_path: str | None = ti.xcom_pull(
        key="reconciliation_path", task_ids="submit_orders"
    )
    trading_date: str = ti.xcom_pull(key="trading_date", task_ids="fetch_ibkr_snapshot")
    reconcile_clean: bool = ti.xcom_pull(key="reconcile_clean", task_ids="durable_reconcile")

    audit_path = artifact_root / "run_audit.json"
    ledger_path = artifact_root.parent / "operational_ledger.jsonl"
    report_path = artifact_root / "operational_report.json"

    audit_argv = [
        "--blotter", blotter_path,
        "--status", "SUBMITTED",
        "--output", str(audit_path),
    ]
    if reconciliation_path and _Path(reconciliation_path).exists():
        audit_argv += ["--reconciliation", reconciliation_path]

    rc_audit = audit_run(argv=audit_argv)
    if rc_audit != 0:
        raise AirflowException("write_ledger: audit record failed; see task log.")

    decision = "COMPLETE" if reconcile_clean else "MONITOR"
    decision_reason = (
        "All paper orders reconciled with broker fill status."
        if reconcile_clean
        else "Broker fill status has UNKNOWN or PARTIAL entries; manual TWS follow-up required."
    )

    ledger_argv = [
        "--trading-date", trading_date,
        "--decision", decision,
        "--decision-reason", decision_reason,
        "--audit", str(audit_path),
        "--ledger", str(ledger_path),
        "--output-report", str(report_path),
    ]
    if reconciliation_path and _Path(reconciliation_path).exists():
        ledger_argv += ["--reconciliation", reconciliation_path]

    rc_ledger = ledger_run(argv=ledger_argv)
    if rc_ledger != 0:
        raise AirflowException("write_ledger: ledger append failed; see task log.")


# ── DAG definition ─────────────────────────────────────────────────────────────

with DAG(
    dag_id="daily_paper_trading",
    default_args=_default_args,
    description=(
        "Daily automated paper-trading pipeline: preflight → target → "
        "snapshot → candidates → risk/compliance → blotter → "
        "approval gate (C1) → submit → reconcile → ledger"
    ),
    schedule_interval="0 23 * * 1-5",
    start_date=_DAG_START_DATE,
    catchup=False,
    max_active_runs=1,
    params={
        "strategy_id": _DEFAULT_STRATEGY_ID,
        "strategy_config": _DEFAULT_STRATEGY_CONFIG,
        "min_delta_weight": _DEFAULT_MIN_DELTA_WEIGHT,
        "whatif_enabled": _DEFAULT_WHATIF_ENABLED,
        "approval_timeout_hours": _DEFAULT_APPROVAL_TIMEOUT_HOURS,
    },
    tags=["paper-trading", "phase-5", "m5.4"],
) as dag:

    # ── Upstream dependency ─────────────────────────────────────────────────
    t_signal_done = ExternalTaskSensor(
        task_id="wait_for_signal_pipeline",
        external_dag_id="daily_signal_pipeline",
        external_task_id="write_simulations",
        execution_delta=timedelta(hours=1, minutes=30),
        timeout=3600,
        poke_interval=120,
        mode="reschedule",
        retries=0,
    )

    # ── Read-only preflight chain ───────────────────────────────────────────
    t_verify = PythonOperator(
        task_id="verify_inputs",
        python_callable=_verify_inputs,
    )

    t_target = PythonOperator(
        task_id="construct_target",
        python_callable=_construct_target,
    )

    t_snapshot = PythonOperator(
        task_id="fetch_ibkr_snapshot",
        python_callable=_fetch_ibkr_snapshot,
        execution_timeout=timedelta(minutes=3),
    )

    t_candidates = PythonOperator(
        task_id="gen_candidates",
        python_callable=_gen_candidates,
    )

    t_risk = PythonOperator(
        task_id="risk_compliance_gate",
        python_callable=_risk_compliance_gate,
        retries=0,  # circuit-breaker failures should not auto-retry
    )

    t_blotter = PythonOperator(
        task_id="build_blotter",
        python_callable=_build_blotter,
        retries=1,
    )

    # ── Optional what-if ────────────────────────────────────────────────────
    t_whatif = PythonOperator(
        task_id="whatif_validate",
        python_callable=_whatif_validate,
        trigger_rule="none_failed",
        retries=2,
        execution_timeout=timedelta(minutes=15),
    )

    # ── C1 approval gate ────────────────────────────────────────────────────
    # Airflow adds /opt/airflow/plugins directly to sys.path, so the plugin is
    # importable as a top-level module (not under airflow.plugins.*).
    # Flat fallback chain using a sentinel: each except only catches ImportError
    # from its own try-block, so a body-level error (AttributeError, SyntaxError)
    # in a candidate module propagates immediately rather than falling through.
    _BlotterApprovalSensor = None
    try:
        from blotter_approval_sensor import BlotterApprovalSensor as _BlotterApprovalSensor  # type: ignore[assignment]
    except ImportError:
        pass
    if _BlotterApprovalSensor is None:
        try:
            from airflow.plugins.blotter_approval_sensor import BlotterApprovalSensor as _BlotterApprovalSensor  # type: ignore[assignment,import]
        except ImportError:
            from airflow_plugins.blotter_approval_sensor import BlotterApprovalSensor as _BlotterApprovalSensor  # type: ignore[assignment,import]
    BlotterApprovalSensor = _BlotterApprovalSensor

    t_approval = BlotterApprovalSensor(
        task_id="wait_approval",
        blotter_run_id_task_id="build_blotter",
        blotter_sha256_task_id="build_blotter",
        poke_interval=300,
        timeout=int(_DEFAULT_APPROVAL_TIMEOUT_HOURS * 3600),
        mode="reschedule",
        retries=0,
        soft_fail=False,
    )

    # ── Submission ──────────────────────────────────────────────────────────
    t_submit = PythonOperator(
        task_id="submit_orders",
        python_callable=_submit_orders,
        retries=1,
        execution_timeout=timedelta(minutes=10),
    )

    # Wait 30 minutes from submission time for limit orders to fill
    # Uses wall-clock elapsed time (XCom-anchored) rather than execution_date
    # so the wait is always 30 minutes from actual broker submission.
    t_wait_fills = PythonOperator(
        task_id="wait_for_fills",
        python_callable=_wait_for_fills,
        retries=0,
        execution_timeout=timedelta(minutes=45),
    )

    t_reconcile = PythonOperator(
        task_id="durable_reconcile",
        python_callable=_durable_reconcile,
        execution_timeout=timedelta(minutes=10),
    )

    t_ledger = PythonOperator(
        task_id="write_ledger",
        python_callable=_write_ledger,
    )

    # ── Dependency graph ─────────────────────────────────────────────────────
    t_signal_done >> t_verify >> t_target >> t_snapshot >> t_candidates
    t_candidates >> t_risk >> t_blotter >> t_whatif >> t_approval
    t_approval >> t_submit >> t_wait_fills >> t_reconcile >> t_ledger

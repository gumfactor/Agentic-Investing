# Airflow DAG Spec — `daily_paper_trading` (M5.4)

**Status:** Draft — awaiting implementation  
**Branch:** `claude/airflow-dag-spec-brqadk`  
**Milestone:** M5.4 (Phase 5 — Automated Paper Trading)  
**Depends on:** M5.1 (Strategy Registry), M5.2 (Trade Journal), M5.3 (Tearsheets)

---

## 1. Purpose

`daily_paper_trading` is the fully automated daily pipeline that takes the
system from fresh market data through to IBKR paper-order submission and
durable reconciliation. It replaces the operator-run step-by-step runbook
(`docs/runbooks/daily_paper_trading.md`) for every step except the mandatory
human approval gate (C1).

The pipeline is the foundation of the 4-week automated paper-trading
qualification (M5.5) required before any live-capital discussion (C8).

### What this DAG does NOT do
- Data ingestion — handled by `daily_data_pipeline` (scheduled 20:00 ET)
- Signal computation — handled by `daily_signal_pipeline` (scheduled 21:30 ET)
- Live trading — `IBKR_PORT=7496` is rejected at every gate

---

## 2. Pipeline Overview

```
[sensor: daily_signal_pipeline.write_scores]
    └── verify_inputs              Step 2  read-only preflight
            └── construct_target   Step 3  equal-weight / MVO target weights
                    └── fetch_ibkr_snapshot  NEW  live IBKR paper positions + cash
                            └── gen_candidates    Step 4  SELL-before-BUY order candidates
                                    └── risk_compliance_gate  Step 5  hard limit gates
                                            └── build_blotter  Step 6  MinIO JSON artifact
                                                    └── whatif_validate  Step 7.5  IBKR what-if (optional)
                                                            └── wait_approval  C1 GATE  BlotterApprovalSensor
                                                                    └── submit_orders  Step 7  IBKR paper submit
                                                                            └── durable_reconcile  Step 8  fill status
                                                                                    └── write_ledger  Step 9  audit + ledger
```

Every task between `verify_inputs` and `build_blotter` is **strictly read-only
with respect to IBKR**. `fetch_ibkr_snapshot` connects to read positions; it
never submits, cancels, or modifies orders.

`submit_orders` is the only task that transmits to the broker, and it requires
the `wait_approval` sensor to have completed successfully (approval stored in
`blotter_approvals` table).

---

## 3. DAG Configuration

```python
dag_id             = "daily_paper_trading"
schedule_interval  = "0 23 * * 1-5"   # 23:00 ET weekdays
start_date         = pendulum.datetime(2026, 7, 1, 23, 0, tz="America/New_York")
catchup            = False             # each run is for "today"; missed days not replayed
max_active_runs    = 1
tags               = ["paper-trading", "phase-5", "m5.4"]
```

**Why 23:00 ET?**  The signal pipeline runs at 21:30 ET and typically completes
in 15–20 minutes. The upstream sensor provides an exact trigger, but 23:00 gives
a 75-minute buffer and keeps the DAG decoupled from signal pipeline retries.

### DAG-level params (overridable per trigger)

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `strategy_id` | str | `"v1_base_momentum"` | Must match a row in `strategies` table (M5.1) |
| `strategy_config` | str | `"config/strategy/v1_base_momentum.yaml"` | Relative path from repo root |
| `min_delta_weight` | float | `0.005` | Minimum weight delta to generate a candidate order |
| `whatif_enabled` | bool | `true` | Run IBKR what-if validation before approval gate |
| `approval_timeout_hours` | float | `8.0` | Hours before approval sensor times out and fails the run |

### Default args

```python
_default_args = {
    "owner": "rqis",
    "depends_on_past": False,
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "on_failure_callback": _alert_operator,  # Slack/email per .env
}
```

### Required environment variables

All gates enforce these or fail immediately. They are never defaulted in code.

| Variable | Required value |
|----------|----------------|
| `PAPER_TRADING` | `"true"` |
| `IBKR_PORT` | `"7497"` |
| `DATABASE_URL` | TimescaleDB connection string |
| `MINIO_ENDPOINT` | MinIO endpoint |
| `MINIO_ACCESS_KEY` | MinIO access key |
| `MINIO_SECRET_KEY` | MinIO secret key |

`PAPER_RUN_CLEARED` must be **unset or false** — any task that detects it set to
`"true"` must raise `AirflowException` immediately (that flag is live-capital
clearance, not paper clearance).

---

## 4. Upstream Dependency Sensor

```python
t_signal_done = ExternalTaskSensor(
    task_id="wait_for_signal_pipeline",
    external_dag_id="daily_signal_pipeline",
    external_task_id="write_scores",
    execution_delta=timedelta(hours=1, minutes=30),  # signal at 21:30, paper at 23:00
    timeout=3600,           # wait up to 1 hour before failing
    poke_interval=120,      # check every 2 minutes
    mode="reschedule",      # release worker slot between pokes
)
```

If the signal pipeline fails or is skipped for the day, this sensor times out
and marks the paper trading run as failed — the operator's alert explains why.

---

## 5. Task Specifications

### 5.1 `verify_inputs` — Step 2 preflight

**Maps to:** `scripts/paper_inputs_check.py` logic  
**Imports:** `scripts.paper_inputs_check.{load_strategy_config, resolve_strategy_id, CheckRecorder}`  
**IBKR connection:** No  
**Retries:** 3

**Inputs (from params/env):**
- `strategy_id`, `strategy_config` DAG params
- `DATABASE_URL` env var

**Checks performed:**
1. Strategy config loads and is syntactically valid
2. `daily_prices` table has at least one row dated within `DEFAULT_MAX_AGE_DAYS` (7) of today
3. `alpha_scores` table has scores for `strategy_id` dated within 7 days of today
4. Enough scored tickers (≥ `portfolio.n_long`) also have prices on the latest score date

**XCom output:**
- `strategy_config_path` — absolute path
- `strategy_id` — resolved ID
- `score_date` — latest score date (ISO string)
- `price_date` — latest price date (ISO string)

**Failure behaviour:** Any `FAIL` from `CheckRecorder` raises `AirflowException`
with a structured message listing all failures.

---

### 5.2 `construct_target` — Step 3 target weights

**Maps to:** `scripts/paper_target_check.py` logic  
**Imports:** `scripts.paper_target_check.construct_target_portfolio`  
**IBKR connection:** No  
**Retries:** 3

**Inputs (XCom):**
- `strategy_config_path`, `strategy_id`, `score_date` from `verify_inputs`

**What it does:**
- Loads strategy config and latest alpha scores for `strategy_id`
- Selects top `portfolio.n_long` tickers by alpha rank
- Applies `portfolio.max_position_weight` cap
- Returns `{ticker: weight}` dict (decimal, sums ≤ 1.0; residual = cash weight)

**XCom output:**
- `target_weights_json` — `{"AAPL": 0.02, "MSFT": 0.02, ...}` (JSON string)

**Failure behaviour:** Unsupported `portfolio.method` values raise immediately
(fail-closed per current implementation).

---

### 5.3 `fetch_ibkr_snapshot` — live IBKR position/cash read

**NEW task — no existing script equivalent**  
**IBKR connection:** Yes (paper port 7497, read-only)  
**Retries:** 3  
**timeout:** 120 seconds per attempt

**Purpose:** Replace the manually-maintained `local/paper_portfolio_snapshot.json`
that the runbook requires the operator to create by hand. Reads actual IBKR paper
account state and writes a snapshot artifact to MinIO.

**What it does:**
1. Verify `PAPER_TRADING=true`, `IBKR_PORT=7497`, reject `PAPER_RUN_CLEARED=true`
2. Instantiate `IBKRBroker(host=..., port=7497, paper=True)`
3. Call `broker.get_positions()` → list of `{ticker, quantity, avg_cost, current_price}`
4. Call `broker.get_cash_balance()` → USD cash available
5. Call `broker.get_nav_usd()` → total NAV in USD (handles CAD→USD via env-var fallback or live FX)
6. Assert NAV > 0 and is finite
7. Build snapshot dict matching `paper_portfolio_snapshot.json` schema:
   ```json
   {
     "schema_version": "paper_portfolio_snapshot.v1",
     "as_of": "2026-07-01",
     "cash": 12500.00,
     "nav_usd": 107500.00,
     "positions": [
       {"ticker": "AAPL", "quantity": 5.0, "price": 210.50}
     ]
   }
   ```
8. Write to MinIO: `rqis-paper/{trading_date}/portfolio_snapshot_{run_id}.json`
9. Disconnect from IBKR

**XCom output:**
- `snapshot_minio_path` — MinIO object path
- `snapshot_json` — snapshot dict as JSON string (small enough for XCom; positions are at most ~500 rows)
- `trading_date` — ISO date string (from `as_of` field)

**Failure behaviour:** Any IBKR connection error, missing NAV, or non-paper broker
response raises `AirflowException`. The task does NOT proceed with a stale
local file.

---

### 5.4 `gen_candidates` — Step 4 order candidates

**Maps to:** `scripts/paper_order_candidates_check.py` logic  
**Imports:** `scripts.paper_order_candidates_check.{build_order_candidates, load_portfolio_snapshot}`  
**IBKR connection:** No  
**Retries:** 3

**Inputs (XCom):**
- `target_weights_json` from `construct_target`
- `snapshot_json` from `fetch_ibkr_snapshot`
- `strategy_config_path`, `score_date` from `verify_inputs`
- `min_delta_weight` from DAG params

**What it does:**
- Loads target weights and current snapshot
- Computes weight deltas: `target_weight - current_weight`
- Generates SELL-before-BUY order candidates for deltas above `min_delta_weight`
- Each candidate includes: ticker, side, estimated_shares, estimated_notional,
  reference_price (latest close), weight_delta

**XCom output:**
- `candidates_json` — list of `OrderCandidate` dicts (JSON string)
- `candidate_count` — int

**Failure behaviour:** Zero candidates (no rebalancing needed today) is a valid
outcome; the task succeeds and pushes an empty list. Downstream tasks handle the
empty-candidates case gracefully (risk gate passes; blotter has zero rows).

---

### 5.5 `risk_compliance_gate` — Step 5 hard-limit gates

**Maps to:** `scripts/paper_risk_compliance_check.py` logic  
**Imports:** `scripts.paper_risk_compliance_check.{GateLimits, GateSummary, _check_candidates, _resolve_limits}`  
**IBKR connection:** No  
**Retries:** 3

**Inputs (XCom):**
- `candidates_json` from `gen_candidates`
- `target_weights_json` from `construct_target`
- `snapshot_json` from `fetch_ibkr_snapshot`
- `strategy_config_path` from `verify_inputs`

**Checks performed:**
1. All candidate fields are finite and non-NaN
2. No short sells unless `allow_shorts=true` in strategy config
3. Sell quantities do not exceed current position sizes in snapshot
4. Per-position concentration ≤ `max_position_weight` from strategy config
5. Max gross target weight ≤ strategy config limit (default 1.0)
6. Optional turnover limit (from DAG param or strategy config)
7. `ComplianceEngine.check()` on each candidate using a **data-only adapter**
   (in-memory, `circuit_breaker_open=False` supplied explicitly, no live OMS,
   no wash-sale history unless supplied as local context in future slice)

**XCom output:**
- `gate_summary_json` — `GateSummary` dict with pass/fail per gate, total candidate count, total notional
- `gate_passed` — bool

**Failure behaviour:** Any gate failure raises `AirflowException` with the full
gate summary JSON in the error message. The operator alert includes the failure
reason so the daily runbook can be consulted.

---

### 5.6 `build_blotter` — Step 6 stage-only blotter artifact

**Maps to:** `scripts/paper_stage_blotter_check.py` logic  
**Imports:** All Step 6 functions from `scripts.paper_stage_blotter_check`  
**IBKR connection:** No  
**Retries:** 1 (blotter artifact must not be duplicated)

**Inputs (XCom):**
- `candidates_json`, `gate_summary_json` from upstream tasks
- `target_weights_json`, `snapshot_json`, `strategy_config_path`, `strategy_id`, `score_date`, `trading_date`

**What it does:**
1. Re-runs all Step 5 gate checks inline to confirm nothing changed between tasks
2. Builds blotter artifact matching `paper_stage_blotter.v1` schema:
   ```json
   {
     "schema_version": "paper_stage_blotter.v1",
     "artifact_type": "paper_stage_only_order_blotter",
     "run_id": "<uuid>",
     "generated_at_utc": "...",
     "paper_only": true,
     "stage_only": true,
     "trading_date": "2026-07-01",
     "score_date": "2026-06-30",
     "snapshot_as_of": "2026-07-01",
     "strategy_id": "v1_base_momentum",
     "strategy_config_sha256": "...",
     "portfolio_input_sha256": "...",
     "gate_input_checksum": "...",
     "candidate_rows": [...],
     "candidate_rows_sha256": "...",
     "risk_compliance_summary": {...},
     "artifact_checksum": "..."
   }
   ```
3. Writes to MinIO: `rqis-paper/{trading_date}/blotter_{run_id}.json`
4. Emits a notification (Slack/email) to the operator with the blotter summary
   and a link to the dashboard approval UI

**XCom output:**
- `blotter_minio_path` — MinIO object path
- `blotter_run_id` — UUID string (stable identifier for approval sensor)
- `blotter_sha256` — hex digest of the artifact file
- `candidate_count` — int

**Failure behaviour:** If the MinIO write fails, retries up to 1× (with a new
`run_id`). The task never partially updates an existing artifact — atomic write
only.

---

### 5.7 `whatif_validate` — Step 7.5 IBKR what-if validation (optional)

**Maps to:** `scripts/paper_whatif_check.py` logic  
**IBKR connection:** Yes (paper port 7497, non-transmitting)  
**Retries:** 2  
**Enabled:** Only when DAG param `whatif_enabled=true`  
**trigger_rule:** `none_failed` (allows prior tasks to have been skipped)

**What it does:**
- Reads blotter artifact from MinIO path
- Connects to IBKR paper, sends each candidate as a what-if order (non-transmitting)
- Reads back estimated commission, margin impact, and liquidity warnings per order
- Writes what-if validation artifact to MinIO: `rqis-paper/{trading_date}/whatif_{run_id}.json`
- Pushes validation summary to dashboard for operator to see before approving

**XCom output:**
- `whatif_minio_path` — MinIO path (None if task skipped)
- `whatif_passed` — bool (None if skipped; downstream tasks treat None as pass)

**Failure behaviour:** IBKR what-if failures are warnings only if they are
non-structural (e.g., single-order margin warning). The task fails only on
connection errors, schema errors, or if IBKR rejects all orders in what-if mode.
A what-if failure blocks the approval gate from opening.

---

### 5.8 `wait_approval` — C1 approval gate (BlotterApprovalSensor)

**New custom Airflow sensor**  
**IBKR connection:** No  
**Retries:** 0 (sensors don't retry; they poke)  
**mode:** `"reschedule"` (releases worker slot between pokes)  
**poke_interval:** 300 seconds (5 minutes)  
**timeout:** `approval_timeout_hours * 3600` (default 28800 = 8 hours)  
**soft_fail:** False (timeout = DAG run FAILED)

**Purpose:** Satisfies safety rule C1. This task will not complete until a human
operator has reviewed the blotter in the dashboard (or via the interim CLI
approval command), selected which orders to submit (per-order checkboxes), and
double-confirmed with their identity recorded.

**How it works:**
1. Reads `blotter_run_id` from XCom (`build_blotter`)
2. Polls `blotter_approvals` table every 5 minutes:
   ```sql
   SELECT selected_order_ids, approved_by, confirmed_blotter_sha256, approved_at_utc
   FROM blotter_approvals
   WHERE blotter_run_id = :run_id
   ```
3. If no row: return False (keep polling)
4. If row found:
   - Verify `confirmed_blotter_sha256 == blotter_sha256` from XCom (checksum
     tamper-detection); mismatch → `AirflowException`, never retry
   - Push `selected_order_ids`, `approved_by`, `approved_at_utc` to XCom
   - Return True (sensor passes)
5. If timeout: task fails → DAG run FAILED → operator alert

**XCom output (on success):**
- `selected_order_ids` — JSON list of candidate identifiers approved for submission
- `approved_by` — operator identifier (email/name)
- `approved_at_utc` — ISO timestamp string

**Security note:** The sensor verifies the SHA-256 of the blotter artifact that
was approved matches the artifact that was generated. If the blotter file was
modified between generation and approval, the sensor raises and the run is aborted.

---

### 5.9 `submit_orders` — Step 7 IBKR paper submission

**Maps to:** `scripts/paper_submit_reconcile_check.py` logic (importable functions)  
**IBKR connection:** Yes (paper port 7497, transmitting)  
**Retries:** 1 (broker idempotency risk; see notes)  
**timeout:** 300 seconds

**Inputs (XCom):**
- `blotter_minio_path`, `blotter_sha256`, `blotter_run_id` from `build_blotter`
- `selected_order_ids` from `wait_approval`
- `approved_by`, `approved_at_utc` from `wait_approval`

**What it does:**
1. Reads blotter artifact from MinIO; verifies checksum against `blotter_sha256`
2. Filters candidate rows to only those in `selected_order_ids`
3. Verifies `PAPER_TRADING=true`, `IBKR_PORT=7497`, rejects `PAPER_RUN_CLEARED=true`
4. Connects to `IBKRBroker`; verifies `broker.is_paper == True` before and after
5. Submits each selected candidate as a limit order using the Step 6 reference prices
6. Records broker order IDs as each response arrives
7. Polls once for initial fill state (most orders will be pending at this hour)
8. Disconnects
9. Writes reconciliation artifact to MinIO: `rqis-paper/{trading_date}/submit_reconciliation_{run_id}.json`
   (created before first submission; updated after each broker response — partial
   failures still produce an audit record)

**XCom output:**
- `reconciliation_minio_path` — MinIO path
- `reconciliation_sha256` — hex digest
- `submitted_count` — number of orders accepted by broker
- `initial_filled_count` — number with immediate fill confirmation

**Retry policy note:** The single retry exists for transient IBKR connection
drops. If the first attempt partially submitted before failing, the retry must
detect which orders already have broker IDs (from the partially-written
reconciliation artifact) and skip re-submitting them. Implementation must read
the partial reconciliation artifact before re-attempting. This logic must be
implemented and tested before the task is used with real capital.

**Failure behaviour:** Any broker error for an individual order is captured in
the reconciliation artifact with status `REJECTED` or `ERROR`; the task
continues submitting remaining orders. If the broker connection fails entirely,
the task fails and the partial artifact is preserved.

---

### 5.10 `durable_reconcile` — Step 8 fill status reconciliation

**Maps to:** `scripts/paper_order_reconcile_check.py` logic  
**IBKR connection:** Yes (paper port 7497, read-only)  
**Retries:** 3  
**execution_timeout:** 600 seconds  
**trigger_rule:** `none_failed`

**Purpose:** Queries IBKR for fill status of each broker order ID recorded in
the Step 7 reconciliation artifact. Runs after a 30-minute delay (implemented as
`TimeDeltaSensor` with `delta=timedelta(minutes=30)` immediately before this task,
or via `wait_for_fills` subtask) to give the market time to process limit orders.

**Timing note:** The 30-minute delay plus submission time puts this task at
approximately 08:00–08:30 ET, giving the operator an accurate fill picture
before the 09:30 ET market open.

**What it does:**
1. Validates Step 7 reconciliation artifact and checksum
2. Verifies `PAPER_TRADING=true`, `IBKR_PORT=7497`, rejects `PAPER_RUN_CLEARED=true`
3. Connects to IBKR, queries current status for each `broker_order_id`
4. Writes order reconciliation artifact to MinIO:
   `rqis-paper/{trading_date}/order_reconciliation_{run_id}.json`
5. Records per-order fill status, quantity filled, average fill price, or error

**XCom output:**
- `order_reconciliation_minio_path` — MinIO path
- `reconciled_count`, `filled_count`, `partial_count`, `unknown_count` — ints

**Failure behaviour:** Per-order query errors are captured as `PARTIAL` in the
artifact; task continues. If all orders return `UNKNOWN` or error, task exits
nonzero after writing the artifact so the operator can follow up manually in TWS.

---

### 5.11 `write_ledger` — Step 9 audit record and operational ledger

**Maps to:** `scripts/paper_operational_ledger_check.py` + `paper_run_audit_check.py` logic  
**IBKR connection:** No  
**Retries:** 3

**What it does:**
1. Writes Step 8 audit record to MinIO: `rqis-paper/{trading_date}/run_audit_{run_id}.json`
   - Includes git branch/commit, command/schema versions, gate statuses, safety assertions
   - `status` = `"SUBMITTED"` if submission attempted; `"SKIPPED"` if zero candidates
2. Appends one JSONL record to the operational ledger:
   `rqis-paper/operational_ledger.jsonl` (append-only, never overwritten — C3)
3. Updates `strategy_runs` table (M5.1) with the day's run record, referencing
   the strategy definition's `config_hash`
4. Pushes a final summary notification (Slack/email) to the operator with the
   day's outcome: candidates submitted, filled, reconciliation status

**XCom output:**
- `audit_minio_path`, `ledger_minio_path` — MinIO paths

---

## 6. Approval Gate — `blotter_approvals` Table

This table is the handshake between the paper trading DAG and the operator
approval UI (dashboard or interim CLI). It must be created via Alembic
migration (C2).

### Schema

```sql
CREATE TABLE blotter_approvals (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    blotter_run_id          TEXT        NOT NULL UNIQUE,
    blotter_minio_path      TEXT        NOT NULL,
    blotter_sha256          TEXT        NOT NULL,
    selected_order_ids      JSONB       NOT NULL,  -- list of candidate identifiers
    approved_at_utc         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_by             TEXT        NOT NULL,  -- operator email or username
    confirmed_blotter_sha256 TEXT       NOT NULL,  -- must match blotter_sha256 exactly
    dashboard_session_id    TEXT,                  -- Streamlit session (future)
    notes                   TEXT
);
```

**Invariants:**
- `blotter_run_id` is unique (one approval per blotter artifact)
- `confirmed_blotter_sha256` is verified by the sensor against the DAG's XCom
  value — a mismatch means the artifact was tampered with and the run is aborted
- This table is append-only (C3): never UPDATE or DELETE rows
- Corrections append a new row with a `correction_of` column referencing the
  original `blotter_run_id` (add that column in a follow-up migration)

### Alembic migration

Create as `infra/migrations/versions/{timestamp}_add_blotter_approvals.py`.
Never run `ALTER TABLE` or `DROP TABLE` directly.

---

## 7. `BlotterApprovalSensor` — Custom Sensor

File: `airflow/plugins/blotter_approval_sensor.py`

```python
from __future__ import annotations

import os
from typing import Any

from airflow.sensors.base import BaseSensorOperator
from airflow.utils.context import Context
from sqlalchemy import create_engine, text


class BlotterApprovalSensor(BaseSensorOperator):
    """Wait for a human to approve the blotter in blotter_approvals table.

    Satisfies safety rule C1. Returns True only when:
      - A matching row exists in blotter_approvals
      - confirmed_blotter_sha256 matches the artifact SHA-256 stored in XCom

    Raises AirflowException (no retry) on SHA-256 mismatch.
    """

    template_fields = ("blotter_run_id_task_id",)

    def __init__(
        self,
        *,
        blotter_run_id_task_id: str,
        blotter_sha256_task_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.blotter_run_id_task_id = blotter_run_id_task_id
        self.blotter_sha256_task_id = blotter_sha256_task_id

    def poke(self, context: Context) -> bool:
        ti = context["ti"]
        blotter_run_id: str = ti.xcom_pull(key="blotter_run_id", task_ids=self.blotter_run_id_task_id)
        expected_sha: str = ti.xcom_pull(key="blotter_sha256", task_ids=self.blotter_sha256_task_id)

        engine = create_engine(os.environ["DATABASE_URL"])
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT selected_order_ids, approved_by, confirmed_blotter_sha256, approved_at_utc "
                    "FROM blotter_approvals WHERE blotter_run_id = :run_id"
                ),
                {"run_id": blotter_run_id},
            ).fetchone()

        if row is None:
            return False  # not yet approved; keep polling

        actual_sha = row.confirmed_blotter_sha256
        if actual_sha != expected_sha:
            raise AirflowException(
                f"Blotter SHA-256 mismatch — artifact may have been tampered with. "
                f"Expected {expected_sha!r}, got {actual_sha!r}. "
                f"Do not retry. Investigate before resubmitting."
            )

        ti.xcom_push(key="selected_order_ids", value=row.selected_order_ids)
        ti.xcom_push(key="approved_by", value=row.approved_by)
        ti.xcom_push(key="approved_at_utc", value=str(row.approved_at_utc))
        return True
```

---

## 8. Interim CLI Approval Command

Until the Streamlit dashboard (M5.8) exists, the operator approves blotters
using a CLI command that writes the `blotter_approvals` row directly.

**New script:** `scripts/paper_approve_blotter.py`

```
python -m scripts.paper_approve_blotter \
    --blotter rqis-paper/2026-07-01/blotter_<run_id>.json \
    --order-ids ALL \
    --operator mshane@thecanadalist.ca
```

The script:
1. Downloads the blotter artifact from MinIO
2. Prints the full order list (all candidates, or filtered by `--order-ids`)
3. Prompts `"Type YES to approve and submit these N orders: "` to stdin
4. On `YES`: computes the artifact SHA-256, inserts a row in `blotter_approvals`
5. Prints confirmation with the `blotter_run_id` and timestamp

`--order-ids ALL` approves all candidates. Individual IDs can be passed as
`--order-ids "AAPL,MSFT,NVDA"` to submit a subset (matching the per-order
checkbox behaviour of the future dashboard).

The script must also run in `--dry-run` mode (prints the order list without
writing to the DB) for operator review without commitment.

---

## 9. MinIO Artifact Layout

All artifacts are stored under the `rqis-paper` bucket with this path structure:

```
rqis-paper/
├── operational_ledger.jsonl            ← append-only (C3); single file, all days
└── {trading_date}/                     ← e.g., 2026-07-01/
    ├── portfolio_snapshot_{run_id}.json
    ├── blotter_{run_id}.json
    ├── whatif_{run_id}.json            ← optional
    ├── submit_reconciliation_{run_id}.json
    ├── order_reconciliation_{run_id}.json
    └── run_audit_{run_id}.json
```

`{run_id}` is the DAG run's `blotter_run_id` (a UUID generated at `build_blotter`
time), shared across all artifacts for a single trading day's pipeline run. This
makes cross-artifact tracing trivial.

**XCom vs MinIO:** Artifacts with >1 MB of content are stored in MinIO; XCom
carries only the path and SHA-256. The blotter candidate rows are typically
~50 rows × ~200 bytes = ~10 KB, which fits in XCom directly — but we store
in MinIO anyway for audit completeness.

---

## 10. Error Handling and Alerting

### DAG-level failure callback

```python
def _alert_operator(context: Any) -> None:
    task_id = context["task_instance"].task_id
    dag_run_id = context["dag_run"].run_id
    exception = context.get("exception")
    trading_date = context["data_interval_end"].in_timezone("America/New_York").date()
    # Send Slack/email via .env-configured webhook
    # Message includes: task_id, trading_date, exception type and first 500 chars
```

### Timeout expectations

| Task | Expected duration | SLA |
|------|-------------------|-----|
| `wait_for_signal_pipeline` | 0–75 min | Times out at midnight |
| `verify_inputs` | < 30 sec | — |
| `construct_target` | < 30 sec | — |
| `fetch_ibkr_snapshot` | 30–90 sec | — |
| `gen_candidates` | < 30 sec | — |
| `risk_compliance_gate` | < 30 sec | — |
| `build_blotter` | < 60 sec | — |
| `whatif_validate` | 2–10 min | — |
| `wait_approval` | 0–8 hours | Must complete by 08:45 ET |
| `submit_orders` | 2–5 min | Must start by 08:45 ET |
| `durable_reconcile` | 2–5 min | Run at ≥ 30 min after submit |
| `write_ledger` | < 30 sec | — |

If `wait_approval` times out, the DAG fails and the operator must manually run
`paper_approve_blotter` and re-trigger the DAG for that date (using
`clear_task_state` to reset from `wait_approval` onward only).

### Circuit breaker guard

The `risk_compliance_gate` task (and `submit_orders`) must check the circuit
breaker table before proceeding. If the circuit breaker is OPEN:
- Raise `AirflowException("Circuit breaker is OPEN — see C4 rule. Do not retry automatically.")`
- The task fails with retries=0 (override `retries` in that specific task)
- The operator alert includes the circuit breaker reason code

Per C4: **never reset the circuit breaker automatically** — only a human with a
reason code may do it.

---

## 11. Task Dependency Graph (code form)

```python
# Sensor
t_signal_done >> t_verify_inputs

# Linear pipeline
t_verify_inputs >> t_construct_target >> t_fetch_snapshot >> t_gen_candidates
t_gen_candidates >> t_risk_gate >> t_build_blotter

# Optional what-if
t_build_blotter >> t_whatif  # skipped if whatif_enabled=False

# Approval gate — both paths converge here
[t_build_blotter, t_whatif] >> t_wait_approval

# Submission and reconciliation
t_wait_approval >> t_submit >> t_wait_fills >> t_durable_reconcile >> t_write_ledger
```

Where `t_wait_fills` is a `TimeDeltaSensor` with `delta=timedelta(minutes=30)`.

---

## 12. Implementation Checklist

### New files to create

| File | Purpose |
|------|---------|
| `airflow/dags/daily_paper_trading.py` | Main DAG |
| `airflow/plugins/blotter_approval_sensor.py` | Custom sensor |
| `scripts/paper_approve_blotter.py` | Interim CLI approval command |
| `infra/migrations/versions/{ts}_add_blotter_approvals.py` | Alembic migration |

### Existing scripts to make importable

The following scripts are currently CLI-only. Their core logic must be refactored
into importable Python functions so the DAG can call them directly (avoiding
`subprocess.run` which loses type safety and error propagation):

| Script | Functions to extract |
|--------|---------------------|
| `scripts/paper_inputs_check.py` | Already importable |
| `scripts/paper_target_check.py` | `construct_target_portfolio` — already importable |
| `scripts/paper_order_candidates_check.py` | `build_order_candidates`, `load_portfolio_snapshot` — already importable |
| `scripts/paper_risk_compliance_check.py` | `_check_candidates`, `_resolve_limits` — already importable (private; consider making public) |
| `scripts/paper_stage_blotter_check.py` | `build_blotter_artifact` — needs extraction from `main()` |
| `scripts/paper_submit_reconcile_check.py` | `submit_blotter`, `load_and_validate_blotter` — needs extraction from `main()` |
| `scripts/paper_order_reconcile_check.py` | `reconcile_orders` — needs extraction from `main()` |
| `scripts/paper_operational_ledger_check.py` | `append_ledger_record` — needs extraction from `main()` |

The refactoring rule: extract a `run(args: Namespace) -> dict` function from
each script's `main()`. The CLI `main()` calls `run(parse_args())`. The DAG
calls `run(args)` directly. No duplication of logic.

### IBKR broker methods required by `fetch_ibkr_snapshot`

Verify these exist on `IBKRBroker` (or add them if missing):
- `get_positions() -> list[dict]`
- `get_cash_balance() -> float`
- `get_nav_usd() -> float` (must handle CAD→USD conversion via env-var fallback)

### Tests to add

| Test | Location |
|------|----------|
| `BlotterApprovalSensor` — approved row found | `tests/airflow/test_blotter_approval_sensor.py` |
| `BlotterApprovalSensor` — SHA-256 mismatch raises | same |
| `BlotterApprovalSensor` — timeout (mocked) | same |
| `paper_approve_blotter` — happy path | `tests/scripts/test_paper_approve_blotter.py` |
| `paper_approve_blotter` — rejects non-YES input | same |
| `daily_paper_trading` DAG loads without import errors | `tests/airflow/test_dag_integrity.py` |
| `fetch_ibkr_snapshot` task with fake broker | `tests/airflow/test_daily_paper_trading.py` |
| `submit_orders` partial retry detection | same |

---

## 13. Deferred / Out of Scope for This Spec

| Item | Where it belongs |
|------|-----------------|
| Dashboard UI for blotter approval (F7.4) | M5.8 Streamlit dashboard spec |
| Live trading path (`IBKR_PORT=7496`) | After M5.5 4-week paper qualification + C8 + C9 |
| Strategy registry integration (`strategy_runs` updates) | Depends on M5.1 schema being merged |
| Multi-strategy DAG runs | Phase 6+ (new YAML configs); one DAG run per strategy |
| Market regime gating | M5.7 |
| Sector/factor exposure reporting in blotter | M5.3 tearsheets |

---

## 14. Timing Diagram (typical weekday)

```
20:00 ET   daily_data_pipeline starts     (data ingestion)
20:15      data_pipeline completes
21:30      daily_signal_pipeline starts   (score computation)
21:45      signal_pipeline completes
23:00      daily_paper_trading starts
23:00      wait_for_signal_pipeline sensor passes immediately
23:01      verify_inputs
23:02      construct_target
23:03      fetch_ibkr_snapshot            (IBKR read; ~90 sec)
23:05      gen_candidates
23:05      risk_compliance_gate
23:06      build_blotter → MinIO artifact written; operator notified
23:08      whatif_validate                (IBKR what-if; ~5 min)
23:13      wait_approval sensor starts    ←── DAG pauses here overnight

[next morning]
07:30      operator reviews blotter in dashboard / CLI
07:32      operator approves → blotter_approvals row inserted
07:32      wait_approval sensor detects approval; passes
07:33      submit_orders                  (IBKR paper submit; ~5 min)
07:38      wait_fills TimeDeltaSensor     (waits 30 min)
08:08      durable_reconcile              (IBKR fill status poll; ~5 min)
08:13      write_ledger                   (audit + ledger append)
08:14      DAG run complete ✓             (75 min before market open)
```

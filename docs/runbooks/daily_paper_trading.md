# Daily Paper Trading Runbook

Operator checklist for the Phase 4 daily IBKR paper-trading workflow. This runbook
is for paper trading only. It does not authorize live capital, live broker config,
bulk order cancellation, or unattended submission.

## Safety Rules

| Gate | Owner | Action |
|------|-------|--------|
| Paper mode | Operator | Use IBKR TWS/Gateway paper on port `7497` only. |
| Secrets | Operator | Do not paste secrets, account passwords, API keys, or `.env` contents into artifacts or logs. |
| Live clearance | Operator | `PAPER_RUN_CLEARED` must be unset or false for every step in this runbook. |
| Human review | Operator | Review the full order list before Step 7. The literal `YES` is allowed only in the Step 7 submission command. |
| Order safety | Operator | Do not cancel all orders from this runbook. Resolve order uncertainty manually in TWS/Gateway one order at a time. |
| Capital safety | Operator | No live capital. This runbook supports the 4-day supervised plumbing rehearsal only; live capital still requires a later 4-week automated paper-trading qualification. |

Stop immediately if any command reports `FAIL`, `BLOCKED`, `UNKNOWN`, `PARTIAL`,
stale inputs, missing broker status, live-mode config, open uncertainty in TWS, or
an artifact checksum mismatch. Record the blocker with Step 8 before retrying.

## Artifact Paths

Use timestamped names for recurring daily runs. Define these once per run and
reuse the variables in the commands below:

```powershell
New-Item -ItemType Directory -Force .\local | Out-Null
$RunStamp = Get-Date -Format yyyyMMdd-HHmmss
$SnapshotPath = ".\local\paper_portfolio_snapshot_$RunStamp.json"
$BlotterPath = ".\local\paper_stage_blotter_$RunStamp.json"
$WhatIfPath = ".\local\paper_whatif_validation_$RunStamp.json"
$SubmitReconciliationPath = ".\local\paper_submit_reconciliation_$RunStamp.json"
$OrderReconciliationPath = ".\local\paper_order_reconciliation_$RunStamp.json"
$AuditPath = ".\local\paper_run_audit_$RunStamp.json"
$LedgerPath = ".\local\paper_operational_ledger.jsonl"
$ReportPath = ".\local\paper_operational_report_$RunStamp.json"
```

The paths below separate the known 2026-06-20 tiny-probe artifacts from the
stamped variables used for new daily runs.

| Purpose | Tiny probe path | New daily run path |
|---------|-----------------|--------------------|
| Portfolio snapshot | `local/paper_portfolio_snapshot.json` | `$SnapshotPath` |
| Step 6 blotter | `local/paper_stage_blotter_small.json` | `$BlotterPath` |
| Step 7.5 what-if | `local/paper_whatif_validation_small.json` | `$WhatIfPath` |
| Step 7 submit/reconcile | `local/paper_submit_reconciliation_small.json` | `$SubmitReconciliationPath` |
| Durable reconciliation | `local/paper_order_reconciliation_small.json` | `$OrderReconciliationPath` |
| Step 8 audit | `local/paper_run_audit_small.json` | `$AuditPath` |
| Daily operational ledger | `local/paper_operational_ledger.jsonl` | `$LedgerPath` |
| Daily operational report | `local/paper_operational_report_small.json` | `$ReportPath` |

Use the tiny probe artifacts when following up the APA/HAL/HPE probe or when the
operator explicitly wants another minimal paper test. Use the full allocation
artifacts only after durable reconciliation is clean and the operator explicitly
chooses to scale beyond the probe. For new daily runs, prefer the `$RunStamp`
variables above instead of the fixed example filenames.

## Preflight Setup

| Owner | Action |
|-------|--------|
| Operator | Start Docker Desktop and the repo services if they are not already running. |
| Operator | Start IBKR TWS/Gateway in paper mode and confirm API access is enabled. |
| Assistant | Run only doc-safe checks unless the operator asks for broker commands. |

```powershell
docker compose up -d

$env:PAPER_TRADING = 'true'
$env:IBKR_PORT = '7497'
Remove-Item Env:\PAPER_RUN_CLEARED -ErrorAction SilentlyContinue
Remove-Item Env:\IBKR_FX_RATE_CAD_USD -ErrorAction SilentlyContinue
Remove-Item Env:\IBKR_FX_RATE_CAD_USD_AS_OF -ErrorAction SilentlyContinue
```

Expected: services start, `PAPER_TRADING` is true, `IBKR_PORT` is `7497`, and
`PAPER_RUN_CLEARED` is absent. Stop if `PAPER_RUN_CLEARED` is true. Leave the
manual FX override unset unless the readiness command fails because IBKR FX
market data is unavailable.

## 1. Data Refresh

| Owner | Action |
|-------|--------|
| Operator | Refresh market data after the market close or before a paper run that needs current inputs. |

```powershell
docker compose exec airflow-scheduler airflow dags trigger daily_data_pipeline
docker compose exec airflow-scheduler airflow dags list-runs -d daily_data_pipeline --limit 5
.\.venv\Scripts\python.exe scripts\check_pipeline_health.py
```

Expected: the latest `daily_data_pipeline` run succeeds and pipeline health is
`OK`. Stop if the DAG is failed/running unexpectedly, duplicate price rows appear,
or `daily_prices` is stale for the intended trading date.

## 2. Score Refresh

| Owner | Action |
|-------|--------|
| Operator | Refresh `v1_base_momentum` alpha scores after fresh prices exist. |

```powershell
docker compose exec airflow-scheduler airflow dags trigger daily_signal_pipeline --conf '{"strategy_id":"v1_base_momentum"}'
docker compose exec airflow-scheduler airflow dags list-runs -d daily_signal_pipeline --limit 5
```

Expected: the latest `daily_signal_pipeline` run succeeds for
`v1_base_momentum`. Stop if factor scoring fails, produces empty scores, or the
next input gate reports stale `alpha_scores`.

## 3. Readiness

| Owner | Action |
|-------|--------|
| Operator | Run the broker readiness gate. Use a fresh CAD-to-USD fallback only when IBKR FX market data is unavailable. |

```powershell
$env:PAPER_TRADING = 'true'
$env:IBKR_PORT = '7497'
Remove-Item Env:\PAPER_RUN_CLEARED -ErrorAction SilentlyContinue
Remove-Item Env:\IBKR_FX_RATE_CAD_USD -ErrorAction SilentlyContinue
Remove-Item Env:\IBKR_FX_RATE_CAD_USD_AS_OF -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m scripts.paper_readiness_check
```

If IBKR FX market data is unavailable for a CAD-denominated account, rerun with
a real numeric same-day fallback:

```powershell
$env:IBKR_FX_RATE_CAD_USD = '0.7400'
$env:IBKR_FX_RATE_CAD_USD_AS_OF = (Get-Date -Format yyyy-MM-dd)
.\.venv\Scripts\python.exe -m scripts.paper_readiness_check
```

Expected: TWS/Gateway paper socket is reachable, the broker reports paper mode,
positions can be read, and USD-equivalent NAV is finite and positive. Stop if
the socket is closed, NAV is zero/missing, FX fallback is stale, or the command
reports live-clearance or non-paper config.

## 4. Portfolio Snapshot

| Owner | Action |
|-------|--------|
| Operator | Create a local snapshot from the readiness output and current TWS paper positions. |
| Assistant | Use the snapshot file only as operator-provided input; do not infer missing broker state. |

```powershell
if (Test-Path $SnapshotPath) {
    throw "Snapshot already exists: $SnapshotPath"
}
@'
{
  "as_of": "YYYY-MM-DD",
  "cash": 1000.0,
  "positions": []
}
'@ | Set-Content $SnapshotPath -Encoding ascii
Get-Content $SnapshotPath
```

Expected: `as_of` is today or the intended paper-run date, `cash` and position
prices are in USD-equivalent terms, quantities are non-negative, and ticker rows
are unique. Stop if the snapshot disagrees with TWS/Gateway or if the paper
account has unresolved orders from a prior run.

## 5. Steps 2-6: Inputs Through Stage-Only Blotter

| Step | Owner | Command | Expected artifact |
|------|-------|---------|-------------------|
| Step 2 | Operator | Validate fresh prices/scores. | Console pass only |
| Step 3 | Operator | Build target weights. | Console target table |
| Step 4 | Operator | Build order candidates from the local snapshot. | Console candidate table |
| Step 5 | Operator | Run risk/compliance checks. | Console pass summary |
| Step 6 | Operator | Write stage-only blotter. | `$BlotterPath` |

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_inputs_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum

.\.venv\Scripts\python.exe -m scripts.paper_target_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum

.\.venv\Scripts\python.exe -m scripts.paper_order_candidates_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum --portfolio-input $SnapshotPath

.\.venv\Scripts\python.exe -m scripts.paper_risk_compliance_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum --portfolio-input $SnapshotPath

.\.venv\Scripts\python.exe -m scripts.paper_stage_blotter_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum --portfolio-input $SnapshotPath --output $BlotterPath
```

Expected: every step passes, Step 6 writes
`artifact_type=paper_stage_only_order_blotter`, `paper_only=true`,
`stage_only=true`, and a checksum-bearing blotter. Stop if inputs are stale,
scores are missing, snapshot age fails, turnover/concentration gates fail, the
output path already exists unexpectedly, or any candidate would require shorting
without explicit reviewed support.

Step 6 does not connect to IBKR, stage OMS orders, submit orders, cancel orders,
reconcile fills, or consume `YES`.

## 6. Step 7.5: What-If Validation

| Owner | Action |
|-------|--------|
| Operator | Validate the exact Step 6 blotter through IBKR paper what-if before any submission. |

Stamped daily run:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_whatif_check --blotter $BlotterPath --output $WhatIfPath
```

Tiny APA/HAL/HPE probe follow-up:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_whatif_check --blotter .\local\paper_stage_blotter_small.json --output .\local\paper_whatif_validation_small.json
```

Expected: all rows are accepted by paper what-if, with zero fractional-share and
zero sub-cent price rows. Stop if any row is rejected, if TWS/Gateway returns a
warning treated as failure, or if the artifact path exists unexpectedly. This
step connects to IBKR paper but does not transmit orders and does not consume
`YES`.

## 7. Step 7: Submit And Reconcile

| Owner | Action |
|-------|--------|
| Operator | First run dry-run display mode and inspect the complete order list. |
| Operator | Submit only if the displayed blotter is exactly the one intended. |

Dry-run display, stamped daily run:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_submit_reconcile_check --blotter $BlotterPath
```

Dry-run display, tiny probe:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_submit_reconcile_check --blotter .\local\paper_stage_blotter_small.json
```

Submit stamped daily run only after explicit operator approval:

```powershell
$reviewed = (Get-FileHash $BlotterPath -Algorithm SHA256).Hash.ToLower()
.\.venv\Scripts\python.exe -m scripts.paper_submit_reconcile_check --blotter $BlotterPath --confirm YES --reviewed-blotter-sha256 $reviewed --output $SubmitReconciliationPath
```

Submit tiny probe only after explicit operator approval:

```powershell
$reviewed = (Get-FileHash .\local\paper_stage_blotter_small.json -Algorithm SHA256).Hash.ToLower()
.\.venv\Scripts\python.exe -m scripts.paper_submit_reconcile_check --blotter .\local\paper_stage_blotter_small.json --confirm YES --reviewed-blotter-sha256 $reviewed --output .\local\paper_submit_reconciliation_small.json
```

Expected: dry-run prints the full order list and stops before broker connection.
Confirmed submission writes a separate `paper_submit_reconciliation` artifact
with broker response details and checksums. Stop if the displayed rows differ
from the intended blotter, if the reviewed SHA-256 changes, if what-if was not
clean, if TWS has unresolved prior orders, or if the operator is not ready to
transmit paper orders.

Do not pass `YES` anywhere else. Do not use `--overwrite` unless intentionally
replacing a failed local artifact after preserving the original.

## 8. Step 8: Audit Record

| Owner | Action |
|-------|--------|
| Operator | Write a final run audit record whether the run was blocked, dry-run only, submitted, failed, or complete. |

Blocked before submission:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_run_audit_check --blotter $BlotterPath --status BLOCKED --blocker "describe the blocker" --output $AuditPath
```

Dry-run only after Step 6 or Step 7 display:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_run_audit_check --blotter $BlotterPath --status DRY_RUN --step1-status PASS --step2-status PASS --step3-status PASS --step4-status PASS --step5-status PASS --output $AuditPath
```

Submitted tiny probe:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_run_audit_check --blotter .\local\paper_stage_blotter_small.json --reconciliation .\local\paper_submit_reconciliation_small.json --status SUBMITTED --step1-status PASS --step2-status PASS --step3-status PASS --step4-status PASS --step5-status PASS --output .\local\paper_run_audit_small.json
```

Submitted stamped daily run:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_run_audit_check --blotter $BlotterPath --reconciliation $SubmitReconciliationPath --status SUBMITTED --step1-status PASS --step2-status PASS --step3-status PASS --step4-status PASS --step5-status PASS --output $AuditPath
```

Expected: the audit artifact records run status, artifact hashes, git metadata,
gate statuses, blockers, safety assertions, and next action. Use `FAILED` only
when Step 7 attempted submission and produced a failed reconciliation artifact.
Use `BLOCKED` for pre-submission issues such as stale scores or dirty broker
state.

Step 8 is audit-only. It validates Step 6 and optional Step 7 artifacts; it does
not validate the durable order reconciliation artifact from the next section.
Retain the durable reconciliation artifact alongside the Step 8 audit record as
separate phase-gate evidence. Step 8 never connects to IBKR, submits orders,
cancels orders, reconciles broker state, mutates prior artifacts, or consumes
`YES`.

## 9. Next-Day Durable Reconciliation

| Owner | Action |
|-------|--------|
| Operator | Reconcile recorded broker order IDs after TWS/Gateway paper is available, especially when initial fill state was inconclusive. |

Tiny APA/HAL/HPE probe:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_order_reconcile_check --reconciliation .\local\paper_submit_reconciliation_small.json --output .\local\paper_order_reconciliation_small.json
```

Stamped daily run:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_order_reconcile_check --reconciliation $SubmitReconciliationPath --output $OrderReconciliationPath
```

Expected: every recorded broker order ID is matched to current paper order/fill
state and a separate `paper_order_reconciliation` artifact is written. The
command is read-only and never submits or cancels orders.

If the durable status is `UNKNOWN` or `PARTIAL`, the command still writes the
artifact but exits nonzero. Treat that as a stop condition: open TWS/Gateway
paper, inspect the specific recorded order IDs manually, and do not scale beyond
the tiny probe until the uncertainty is resolved and documented.

## 10. Daily Operational Ledger

| Owner | Action |
|-------|--------|
| Operator | Append one local ledger record for the daily decision and retained artifacts. |

Blocked day:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_operational_ledger_check --trading-date (Get-Date -Format yyyy-MM-dd) --decision BLOCKED --decision-reason "describe the blocker" --audit $AuditPath --ledger $LedgerPath --output-report $ReportPath
```

Dry-run day:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_operational_ledger_check --trading-date (Get-Date -Format yyyy-MM-dd) --decision DRY_RUN --decision-reason "paper blotter reviewed but not submitted" --audit $AuditPath --ledger $LedgerPath --output-report $ReportPath
```

Submitted day that still needs durable reconciliation:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_operational_ledger_check --trading-date (Get-Date -Format yyyy-MM-dd) --decision SUBMITTED --decision-reason "paper orders submitted; durable reconciliation pending" --audit $AuditPath --reconciliation $SubmitReconciliationPath --ledger $LedgerPath --output-report $ReportPath --circuit-breaker-event "no circuit breaker events observed"
```

Failed submission day:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_operational_ledger_check --trading-date (Get-Date -Format yyyy-MM-dd) --decision FAILED --decision-reason "paper submission attempted and failed; inspect Step 7 reconciliation" --audit $AuditPath --reconciliation $SubmitReconciliationPath --ledger $LedgerPath --output-report $ReportPath
```

Complete day after clean durable reconciliation:

```powershell
.\.venv\Scripts\python.exe -m scripts.paper_operational_ledger_check --trading-date (Get-Date -Format yyyy-MM-dd) --decision COMPLETE --decision-reason "all recorded paper broker IDs reconciled" --audit $AuditPath --reconciliation $SubmitReconciliationPath --order-reconciliation $OrderReconciliationPath --ledger $LedgerPath --output-report $ReportPath
```

Expected: the command validates the supplied artifacts and appends one
checksum-bearing JSONL record to `$LedgerPath`, including the daily decision,
artifact hashes, submitted order records, reconciled fill records when
available, and any circuit-breaker notes. The optional report is a compact JSON
summary for quick review. The ledger command never connects to IBKR, submits or
cancels orders, resets/trips circuit breakers, mutates prior artifacts, or
consumes `YES`. Use `MONITOR` instead of `COMPLETE` when durable reconciliation
is still `UNKNOWN` or `PARTIAL`.

## Completion Criteria

| Status | Meaning | Next action |
|--------|---------|-------------|
| `BLOCKED` | A pre-submission gate failed or inputs were stale. | Fix the blocker, rerun from the earliest affected step, and write a new audit. |
| `DRY_RUN` | Blotter was generated/reviewed but not submitted. | Preserve artifacts; decide whether to what-if/submit later. |
| `SUBMITTED` | Paper orders were transmitted and a Step 7 artifact exists. | Run durable reconciliation before scaling. |
| `FAILED` | Step 7 attempted submission and produced a failed reconciliation artifact. | Inspect TWS manually; do not retry blindly. |
| `COMPLETE` | Operational label for a submitted run whose separate durable reconciliation artifact is clean. Step 8 does not prove this by itself. | Retain the Step 8 audit, durable reconciliation, and ledger/report artifacts together for the 4-day supervised plumbing rehearsal. |

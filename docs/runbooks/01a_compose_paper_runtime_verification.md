# Gate 01A — Compose Paper Runtime Operator Verification Checklist

Companion to `docs/plans/01a-compose-paper-runtime-checklist.md` (the
authoritative BUG-001..BUG-004 spec). This document is the **operator-run**
half of that checklist: the steps that require a live TWS/IB Gateway paper
session and a real Docker Desktop stack, which an automated agent session
must not perform (no broker connections, no DAG triggers, no order
submission of any kind — see the plan's Guardrails section).

Everything below is read-only or additive-only. Nothing here submits,
stages, cancels, or reconciles a broker order, and nothing here triggers
`daily_paper_trading`. Completing this checklist marks **Gate 01A** done; it
does not authorize a paper DAG run (see "Exit criteria" in the plan) and it
is separate from the `docs/runbooks/airflow_fire_drill.md` recovery drill.

Automated evidence already collected for BUG-001/002/003 during
implementation (see `Worklog.md` and the branch's commit messages for exact
commands/output):
- `docker compose config` renders `PAPER_TRADING=true`, `IBKR_PORT=7497`,
  `IBKR_HOST=host.docker.internal`, `IBKR_CLIENT_ID=1`,
  `RQIS_PAPER_ARTIFACT_DIR=/opt/airflow/rqis_paper`, and
  `RQIS_RUNTIME_CONTEXT=compose_bridged` for `airflow-init`,
  `airflow-webserver`, and `airflow-scheduler`.
- `docker build -f infra/docker/Dockerfile.airflow .` succeeds; the
  build-time gate confirms `airflow version` runs, no Airflow-critical
  package drifted from the base image, and `pip check` shows only the
  documented `snowflake-connector-python`/`cffi` mismatch.
- `infra/docker/smoke_test_dag_imports.py` imports `daily_paper_trading`,
  `daily_signal_pipeline`, `daily_data_pipeline`, and every module reached
  through (and slightly past) the C1 approval gate inside the built image,
  and confirms `airflow.__file__` resolves to the installed package.
- A container-write / host-read sentinel round trip on the
  `RQIS_PAPER_ARTIFACT_HOST_DIR` bind mount matched SHA-256 on both sides.

The steps below are what still requires the operator's own machine and TWS
session.

---

## Preconditions

- [ ] Docker Desktop running. Record its version (`docker --version` /
  `docker compose version`) and whether this is Windows Docker Desktop or
  Linux Docker Engine (Gate 01A targets Windows Docker Desktop; see the
  plan's open decision 3 for the Linux `host-gateway` alternative).
- [ ] TWS or IB Gateway running in **paper** mode (port `7497`), with its API
  socket enabled and "Read-Only API" left in its default/safe state. Do not
  enable order submission from an unattended source.
- [ ] `.env` populated from `.env.example` with real (non-committed) values
  for `POSTGRES_PASSWORD`, `AIRFLOW_FERNET_KEY`, `AIRFLOW_ADMIN_PASSWORD`,
  `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `GRAFANA_PASSWORD`. Confirm
  `PAPER_TRADING=true`, `IBKR_PORT=7497`, `IBKR_HOST=127.0.0.1` (host-side
  value), `IBKR_HOST_AIRFLOW=host.docker.internal` (or your verified Linux
  gateway alternative), and `RQIS_PAPER_ARTIFACT_HOST_DIR` set to a real
  local path (default `./local/paper_artifacts`).
- [ ] Never paste `.env` contents, account numbers, or TWS output containing
  balances/positions into a shared evidence file. Redact before saving.

## 1. Bring up the stack and confirm the Compose contract (BUG-001)

```powershell
docker compose build airflow-webserver airflow-scheduler airflow-init
docker compose up -d
docker compose config > $env:TEMP\rqis_compose_config_redacted.txt
```

- [ ] Open the temp file and redact `POSTGRES_PASSWORD`, `AIRFLOW_FERNET_KEY`,
  `AIRFLOW_ADMIN_PASSWORD`, `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`,
  `GRAFANA_PASSWORD` before keeping it as evidence (or discard the file
  entirely once you've visually confirmed the paper-runtime variables).
- [ ] Confirm `airflow-init`, `airflow-webserver`, and `airflow-scheduler`
  each show `PAPER_TRADING: "true"`, `IBKR_PORT: "7497"`,
  `IBKR_HOST: host.docker.internal` (or your Linux equivalent),
  `RQIS_RUNTIME_CONTEXT: compose_bridged`.
- [ ] `docker compose ps` shows `airflow-webserver` and `airflow-scheduler`
  healthy/running, and `airflow-init` exited `0`.

## 2. Confirm DAGs are visible and paused as intended

```powershell
docker compose exec airflow-scheduler airflow dags list
```

- [ ] `daily_data_pipeline`, `daily_signal_pipeline`, and
  `daily_paper_trading` are all listed with zero import errors.
- [ ] Confirm each DAG's paused state matches intent (all three are
  `AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION: "true"` by default — leave
  `daily_paper_trading` paused until the operator is ready to run the 4-week
  automated qualification per C8).
- [ ] Do **not** trigger `daily_paper_trading` from this checklist.

```powershell
docker compose exec airflow-scheduler airflow dags list-import-errors
```

- [ ] Output is empty (no import errors for any DAG).

## 3. Broker reachability preflight from the Airflow network context (BUG-004)

Run the existing read-only paper-readiness check **from inside the Airflow
container**, not just from the Windows host — this is the check that
actually exercises the `IBKR_HOST=host.docker.internal` path against your
running TWS/Gateway paper session.

```powershell
docker compose exec `
  -e PAPER_TRADING=true -e IBKR_PORT=7497 `
  airflow-scheduler python -m scripts.paper_readiness_check
```

- [ ] Confirms paper mode, socket reachability, and reads NAV/positions
  read-only (no orders). Redact and save only non-account-identifying
  portions of the output as evidence.
- [ ] If this fails with a connection/timeout error, check: TWS API socket
  enabled, TWS "Trusted IPs" allows the Docker bridge network (or is left
  unrestricted for local development), Windows Firewall allows inbound
  connections to TWS's port from the Docker Desktop VM.

### 3a. Confirm the failure paths (already covered by automated tests; optional live cross-check)

- [ ] `execution/tests/test_ibkr_broker_endpoint_fail_closed.py` and
  `execution/tests/test_ibkr_bridged_host_validation.py` cover the
  unresolvable-host and port-7496 failure paths with fakes. No live
  cross-check is required unless you want to confirm the same behavior
  against a real (but wrong) endpoint, e.g. temporarily setting
  `IBKR_HOST_AIRFLOW` to an address you know is unreachable and re-running
  step 3 above, expecting a clear connection failure rather than a hang or a
  silent success.

## 4. Shared artifact storage — restart persistence (BUG-003)

The automated `tests/infra/test_paper_artifact_shared_storage.py` suite
proves a container write is host-readable with a matching SHA-256. This
step additionally proves the artifact survives an `airflow-scheduler`
container restart, which requires a live stack:

```powershell
docker compose exec airflow-scheduler bash -c `
  "mkdir -p `$RQIS_PAPER_ARTIFACT_DIR/01a_restart_check && echo rqis-restart-sentinel > `$RQIS_PAPER_ARTIFACT_DIR/01a_restart_check/sentinel.txt"
Get-Content .\local\paper_artifacts\01a_restart_check\sentinel.txt
docker compose restart airflow-scheduler
Get-Content .\local\paper_artifacts\01a_restart_check\sentinel.txt
```

- [ ] The sentinel content is identical before and after the restart.
- [ ] Clean up: `Remove-Item -Recurse .\local\paper_artifacts\01a_restart_check`.

## 5. Sign-off

- [ ] Record commit SHA, branch (`dev/R2-01A-compose-runtime` or its merge
  target), Docker Desktop version, and completion date in `Worklog.md`.
- [ ] Update the BUG-001..BUG-004 rows in `bugs.md` from "Open" to
  "Implemented — pending operator verification" (agent-side) or
  "Verified" (once every checkbox above is checked by the operator).
- [ ] Confirm Gate 01A is complete per the plan's exit criteria: all four BUG
  acceptance statements pass on a fresh Compose build. Gate 02A (an actual
  no-submit DAG run proving migrations + DAG imports + shared artifacts +
  IBKR reachability together) remains a separate, later exercise.

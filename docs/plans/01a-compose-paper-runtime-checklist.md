# 01A — Compose Paper Runtime Implementation Checklist

**Codex session ID:** `019f577d-e183-7280-8f37-d8be8858d120`

**Roadmap task:** Make Compose paper runtime executable
**Scope:** BUG-001 through BUG-004
**Completion boundary:** a Compose-managed Airflow runtime can import the DAG,
pass its paper-mode gate, persist a test artifact to shared storage, and perform
an explicitly no-submit broker-connectivity preflight. This task does **not**
authorize an order submission or satisfy Gate 02A.

## Guardrails

- Keep `PAPER_TRADING=true`, `IBKR_PORT=7497`, and leave
  `PAPER_RUN_CLEARED` unset/false throughout all checks.
- Never put broker credentials, account identifiers, or `.env` contents in the
  checklist evidence, test fixtures, logs, or commits.
- Do not trigger `daily_paper_trading`, approve a blotter, or execute the
  `submit_orders` task for this task.
- Stop on an unexpected broker mode, a live port, an unresolvable host, or a
  failed health/dependency/import check.

## Preconditions

- [ ] Record the current commit, dirty-worktree state, Docker Desktop version,
  and whether the target is Windows Docker Desktop or Linux Docker Engine.
- [ ] Confirm that the operator has started TWS/IB Gateway in **paper** mode,
  enabled its API socket, and has authorized a read-only connectivity check.
- [ ] Confirm the intended host address. Use `host.docker.internal` for Windows
  Docker Desktop; a Linux deployment must instead supply an explicit gateway
  or the Compose `host-gateway` mapping. Do not silently retain `127.0.0.1`.
- [ ] Treat an unset, empty, `localhost`, or `127.0.0.1` `IBKR_HOST` as a
  configuration error in a bridged Compose runtime. Permit it only when a
  host-networked deployment is explicitly declared and tested.
- [ ] Create a redacted evidence directory outside version control (for
  example `local/qualification/01a-<timestamp>/`).

## 1. Define the Compose runtime contract (BUG-001, BUG-004)

- [ ] Add the following variables to the shared Airflow Compose environment so
  they reach `airflow-init`, `airflow-webserver`, and `airflow-scheduler`:
  `PAPER_TRADING`, `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, and
  `RQIS_PAPER_ARTIFACT_DIR`.
- [ ] Set the values through `.env` substitution; do not hard-code an account,
  host address, or credentials in `docker-compose.yml`.
- [ ] Update `.env.example` and the relevant operator documentation to show the
  Docker-safe host setting and explain the Linux alternative. Preserve a
  safe local default only if it cannot be mistaken for a container endpoint.
- [ ] For Linux compatibility, add a conditional/portable `host-gateway`
  mapping only if it is supported by the target Compose environment; otherwise
  document the explicit gateway-IP requirement.
- [ ] Add a test that renders/inspects the Airflow service environment and
  verifies the paper variables are present, with `PAPER_TRADING=true` and
  `IBKR_PORT=7497` in the test fixture.
- [ ] Add fail-closed tests for unset, empty, `localhost`, and `127.0.0.1`
  `IBKR_HOST` values in bridged Compose. The tests must demonstrate that an
  empty value cannot fall back to `IBKRBroker`'s container-local default.
- [ ] Enforce that same bridged-runtime endpoint validation in production before
  constructing `IBKRBroker`; a test-only guard is insufficient. Allow the
  loopback exception only for an explicitly declared and tested host-network
  runtime.

**Acceptance evidence:** `docker compose config` shows the required variables
for every Airflow service, and an in-container invocation of
`_require_paper_env()` succeeds with the rendered environment. Never retain
raw `docker compose config` output: write it only to a temporary local file,
redact every secret-valued field before saving evidence, and verify the saved
copy contains no passwords, keys, or connection-string credentials. Retain only
the non-secret settings needed to demonstrate the paper-runtime contract.

## 2. Make the Airflow image a real DAG runtime (BUG-002)

- [ ] Inventory the imports reached by `daily_paper_trading` and its task
  helpers, including Airflow providers, `pandas`, `pyarrow`, `ib_insync`,
  SQLAlchemy/database drivers, MinIO, and project modules.
- [ ] Reconcile that inventory with the base Airflow image, `requirements.txt`,
  and `pyproject.toml`. Resolve the Python version mismatch before choosing an
  installation command: the image is Python 3.11 while the project declares
  Python 3.12 or newer.
- [ ] Make one explicit dependency decision: either move the Airflow image to a
  supported project Python version, or lower/justify the project requirement.
  Do not mask the incompatibility with an unconstrained install.
- [ ] Select and record an Airflow-version-compatible constraints or lock-file
  strategy before installing project requirements. Do not allow a generic
  `pip install -r requirements.txt` to replace packages supplied by the base
  Airflow image without compatibility verification.
- [ ] Install the pinned runtime dependencies and the project package (or
  otherwise make the project modules importable) in the image build.
- [ ] Build the image without relying on host `PYTHONPATH` or host-installed
  packages.
- [ ] Add a container smoke test that imports `daily_paper_trading` and imports
  every module needed before the approval gate. It must fail on missing
  dependencies.
- [ ] Make the smoke test assert that `airflow.__file__` resolves to the base
  image's installed Apache Airflow package (not this repository's local
  `airflow` test stubs), and record the installed Airflow version.
- [ ] In the fresh image, run `airflow version` and `pip check`; retain their
  redacted output and fail the build if either reports an incompatible result.

**Acceptance evidence:** a clean `docker compose build` succeeds, and the
fresh image passes the import smoke command without `ModuleNotFoundError` or a
Python-version incompatibility.

## 3. Persist artifacts in shared Compose storage (BUG-003)

- [ ] Choose a named volume or bind mount for `RQIS_PAPER_ARTIFACT_DIR`. It
  must be mounted at the identical in-container path for every Airflow service
  that reads or writes run artifacts.
- [ ] Identify the dashboard service/host-side approval surface that will read
  those artifacts. If no Compose dashboard service exists, choose a host bind
  mount and document the host path plus access permissions; do not call an
  Airflow-only volume “shared.”
- [ ] Ensure the container user can create a per-run directory and write an
  artifact without elevated permissions.
- [ ] Add a smoke test that writes a harmless sentinel under
  `{RQIS_PAPER_ARTIFACT_DIR}/<safe-run-id>/` from an Airflow container and
  reads the same bytes from the approval/dashboard surface.
- [ ] Confirm the sentinel remains readable after restarting the scheduler.

**Acceptance evidence:** recorded SHA-256 and path for the sentinel match on
both surfaces, and the post-restart read succeeds.

## 4. Prove safe broker reachability (BUG-004)

- [ ] Run a container-local TCP/API preflight against the configured
  `IBKR_HOST:7497`; retain only redacted command output.
- [ ] Run the existing paper-readiness check from the same network context as
  Airflow (not just from the Windows host). It must confirm paper mode and
  perform only the documented read-only account checks.
- [ ] Add a test covering the unsafe default: `127.0.0.1` must not be accepted
  as the Compose broker endpoint unless the runtime is explicitly host-networked.
- [ ] Test failure paths with an unavailable hostname/socket and with port
  `7496`; both must stop before any DAG task can submit an order.

**Acceptance evidence:** the configured paper endpoint passes the read-only
container preflight; unreachable and live-port fixtures fail closed.

## 5. Verification and documentation closeout

- [ ] Run the focused unit tests for the DAG/environment checks and any added
  Compose contract tests.
- [ ] Run `docker compose config`, build the image, bring up the stack, and
  check Airflow health plus DAG import errors.
- [ ] Verify both `daily_signal_pipeline` and `daily_paper_trading` are listed
  and unpaused/paused only as intended; do not trigger a paper DAG run here.
- [ ] Update `docs/runbooks/airflow_fire_drill.md`: it must name only actual
  Compose services (there is no `airflow-worker` under `LocalExecutor`) and
  state that its recovery drill is separate from Gate 01A.
- [ ] Reconcile `docs/airflow_paper_dag_spec.md` with the implemented DAG's
  local shared-artifact contract. It currently describes MinIO artifact writes;
  either update it to the verified `RQIS_PAPER_ARTIFACT_DIR` design or label it
  superseded with a link to the authoritative implementation/runbook.
- [ ] Update the daily-paper runbook only where its host/artifact assumptions
  changed; remove obsolete local-path instructions only after their replacement
  is operationally verified.
- [ ] Save redacted build, import, environment-gate, shared-artifact, and
  broker-preflight evidence. Link its location in `Worklog.md` when the task is
  complete.

## Exit criteria

Mark 01A delivered only when all four BUG acceptance statements above pass on a
fresh Compose build. Keep 02A blocked until a separate no-submit DAG run proves
migrations, DAG imports, shared artifacts, and IBKR reachability together.

## Open decisions to resolve during implementation

1. Which image/runtime Python version is authoritative: the current Airflow
   3.11 image or the project’s declared 3.12 minimum?
2. Is the artifact consumer a future Compose dashboard container or a host-side
   approval tool? This determines named volume versus host bind mount.
3. Is Gate 01A initially supported only on Windows Docker Desktop, or must the
   same Compose file support Linux immediately? This determines the broker-host
   mapping contract.

# Adversarial Review Findings

Review date: 2026-06-30

This file consolidates an adversarial, multi-theme review of the project. It is intentionally written as a handoff queue for later remediation; no fixes are included here.


## How to use this running tally

`bugs.md` is the canonical running tally of known fixes that need to happen before this system can be considered hardened for larger paper-trading scale or any live-capital discussion. Treat it as a living remediation queue:

- Add every newly discovered defect, weakness, or deferred fix as a numbered `BUG-XXX` entry.
- Do not delete fixed items silently. When a bug is fixed, mark its status in the roadmap and reference the fixing PR/commit so the audit trail remains intact.
- Keep each item classified by category, severity, implementation priority, and short/medium/long-term fix horizon.
- Re-triage after each major milestone, adversarial review, or production/paper incident.

### Triage taxonomy

| Field | Values | Meaning |
|-------|--------|---------|
| Category | `Infra/Deploy`, `Trading Safety`, `Security/Auth`, `Dashboard/API`, `Risk`, `Research/Signals`, `Data/Storage`, `Backtesting`, `Portfolio`, `Packaging/CI`, `Docs/Process` | Primary subsystem or ownership area. |
| Severity | `P0`, `P1`, `P2`, `P3` | User/system impact if unfixed. `P0` blocks safe operation or invalidates core results; `P1` is high impact; `P2` is medium impact; `P3` is low impact. |
| Fix priority | `F0`, `F1`, `F2`, `F3` | Implementation ordering. `F0` is immediate stop-the-line work; `F1` should be next-sprint hardening; `F2` is planned medium-term work; `F3` is backlog/cleanup. |
| Horizon | `Short`, `Medium`, `Long` | Short-term fixes should happen before the next serious paper/live operational expansion; medium-term fixes belong in the next hardening phase; long-term fixes are lower-risk backlog items. |
| Status | `Open`, `In Progress`, `Implemented — pending operator verification`, `Fixed`, `Deferred`, `Won't Fix` | Current remediation state. New entries default to `Open`. `Implemented — pending operator verification` means the code/test change is complete and merged to the working branch, but a step requiring a live external dependency (e.g. a real TWS/IB Gateway session) that an automated agent session must not perform is still outstanding before the item can be marked `Fixed`. |

### Fix implementation roadmap

#### Short-term / stop-the-line and near-term hardening

| Bug | Category | Severity | Fix priority | Status | Short rationale |
|-----|----------|----------|--------------|--------|-----------------|
| BUG-001 | Infra/Deploy | P0 | F0 | Fixed | Airflow paper DAG cannot pass its own env gate in Compose. |
| BUG-002 | Infra/Deploy | P0 | F0 | Fixed | Airflow image omits runtime dependencies used by DAGs. |
| BUG-003 | Infra/Deploy | P0 | F0 | Fixed | Paper artifacts are written to an unmounted container path. |
| BUG-004 | Infra/Deploy | P0 | F0 | Fixed | IBKR host defaults to container-local localhost. |
| BUG-005 | Trading Safety | P0 | F0 | Fixed | Approval quantity overrides can be tampered upward. |
| BUG-006 | Trading Safety | P0 | F0 | Fixed | Corrupt reconciliation artifacts can cause duplicate orders. |
| BUG-007 | Risk | P0 | F0 | Fixed | Risk dashboard can report zero/incorrect risk from schema mismatch. |
| BUG-008 | Research/Signals | P0 | F0 | Fixed | Current-membership universe creates survivorship leakage. |
| BUG-009 | Research/Signals | P0 | F0 | In Review | Same-close signal/return timing can introduce lookahead. |
| BUG-010 | Research/Signals | P0 | F0 | Fixed | `pct_change()` defaults can distort many indicators. |
| BUG-011 | Security/Auth | P1 | F1 | Open | Approval gate trusts any matching DB row. |
| BUG-012 | Trading Safety | P1 | F1 | Open | Circuit breaker is UI-local and not enforced by Airflow submission. |
| BUG-013 | Security/Auth | P1 | F1 | Open | Host-published services and weak auth create compromise paths. |
| BUG-014 | Security/Auth | P1 | F1 | Open | Dashboard approval identity is spoofable/unknown. |
| BUG-015 | Dashboard/API | P1 | F1 | Open | Blotter UI can approve the wrong pending run. |
| BUG-016 | Dashboard/API | P1 | F1 | Open | Blotter UI does not validate full schema before approval. |
| BUG-017 | Trading Safety | P1 | F1 | Fixed | Quantity reduction updates one field while validation checks another. |
| BUG-036 | Packaging/CI | P0 | F0 | Fixed | Invalid PEP 517 backend blocks package builds. |
| BUG-037 | Data/Storage | P1 | F1 | Open | Same-date corporate actions overwrite one another. |
| BUG-038 | Data/Storage | P1 | F1 | Open | Snapshot version paths are mutable. |
| BUG-039 | Backtesting | P1 | F1 | Open | Object-store failures can become unadjusted backtests. |
| BUG-040 | Trading Safety | P1 | F1 | Fixed | Wash-sale guard checks the wrong order direction. |
| BUG-055 | Trading Safety | P0 | F0 | Fixed | prices_json=None crashes _write_simulation before error handler, blocking ExternalTaskSensor. |
| BUG-056 | Docs/Process | P1 | F1 | Fixed | wash_sale_context docstring says "SELL-side tickers" after BUG-040 fix renamed to BUY-side. |
| BUG-057 | Trading Safety | P1 | F1 | Fixed | bool is int subclass; True passes isinstance(override_qty, int) and submits 1 share silently. |
| BUG-058 | Trading Safety | P1 | F1 | Fixed | Second reconciliation artifact read swallows exceptions, truncating audit trail on retry. |
| BUG-059 | Research/Signals | P1 | F1 | Fixed | simulated_return divides by len(returns) not n_long, overstating NAV when tickers lack prior-day data. |
| BUG-060 | Trading Safety | P1 | F1 | Fixed | No test for fail-safe BUY rejection when recent_loss_sells set but as_of_date absent. |
| BUG-041 | Risk | P1 | F1 | Open | Sector concentration is computed but not breach-checked. |
| BUG-042 | Trading Safety | P1 | F1 | Open | IBKR order-ID timeout can leave live order untracked. |
| BUG-043 | Packaging/CI | P1 | F1 | Open | Test collection can fail through MLflow/pkg_resources drift. |
| BUG-044 | Packaging/CI | P1 | F1 | Open | Package discovery excludes operational modules. |
| BUG-045 | Packaging/CI | P1 | F1 | Open | Local Airflow stubs shadow real Airflow imports. |

#### Medium-term / planned hardening

| Bug | Category | Severity | Fix priority | Status | Short rationale |
|-----|----------|----------|--------------|--------|-----------------|
| BUG-018 | Infra/Deploy | P1 | F2 | Open | Project Python metadata conflicts with Airflow image. |
| BUG-019 | Infra/Deploy | P1 | F2 | Open | MLflow bucket config is ignored by server command. |
| BUG-020 | Data/Storage | P1 | F2 | Open | Raw snapshot path logging ignores custom bucket names. |
| BUG-021 | Infra/Deploy | P1 | F2 | Open | Alembic migrations assume extensions exist externally. |
| BUG-022 | Infra/Deploy | P1 | F2 | Open | DB init scripts are one-shot but required for secondary DBs. |
| BUG-023 | Research/Signals | P1 | F2 | Open | PEG inverse rewards double-negative EPS/growth cases. |
| BUG-024 | Research/Signals | P1 | F2 | Open | Fundamental PIT safety is delegated to input dates. |
| BUG-025 | Research/Signals | P1 | F2 | Open | Missing-data renormalization creates coverage-driven alpha. |
| BUG-026 | Research/Signals | P1 | F2 | Open | Indicator z-scoring drops all-tie/one-name dates. |
| BUG-027 | Dashboard/API | P2 | F2 | Open | Approval UI can crash on malformed/null quantities. |
| BUG-028 | Dashboard/API | P2 | F2 | Open | Artifact scanning lacks strong containment checks. |
| BUG-029 | Trading Safety | P2 | F2 | Open | Live-clearance env var names differ across components. |
| BUG-030 | Trading Safety | P2 | F2 | Fixed | Airflow retries are risky for broker submission/reconcile/ledger tasks. |
| BUG-031 | Research/Signals | P2 | F2 | Open | Fundamental growth uses daily-row shifts after forward-fill. |
| BUG-032 | Data/Storage | P2 | F2 | Open | `pivot_table()` silently averages duplicate records. |
| BUG-033 | Infra/Deploy | P2 | F2 | Open | Prometheus scrape target is not backed by Compose service. |
| BUG-046 | Data/Storage | P2 | F2 | Open | Market-data backfill can mark partial loads complete. |
| BUG-047 | Data/Storage | P2 | F2 | Open | Data-quality flag dedupe has no conflict key. |
| BUG-048 | Trading Safety | P2 | F2 | Open | Trade-fill dedupe allows duplicate cumulative fills. |
| BUG-049 | Portfolio | P2 | F2 | Open | Optimizer fallbacks can violate configured caps. |
| BUG-050 | Risk | P2 | F2 | Open | NaN-heavy return series can suppress VaR/CVaR breaches. |
| BUG-051 | Trading Safety | P2 | F2 | Fixed | Step 7 CLI can submit old checksum-valid blotters. |
| BUG-052 | Docs/Process | P2 | F2 | Fixed | Fire-drill runbook contradicts DAG timezone semantics; inline schedule_interval comment also wrong. |
| BUG-053 | Packaging/CI | P2 | F2 | Fixed | `make check` mutates the working tree. |
| BUG-064 | Research/Signals | P2 | F2 | Fixed | `_write_simulation` only processes current-run strategy from XCom; shadow strategies are skipped. |
| BUG-065 | Research/Signals | P2 | F2 | Fixed | `simulated_return` divides by n_long when universe < n_long, understating returns for small strategies. |
| BUG-066 | Research/Signals | P2 | F2 | Open | Cross-sectional scoring has no minimum-eligible-count gate; full-window suppression increases silent cross-section shrinkage. |
| BUG-067 | Data/Universe | P1 | F1 | Fixed (dev/R2-01B2-pit-universe) | `config/universe_loader.py` returned an empty universe on Wikipedia fetch failure (fail-open). |
| BUG-068 | Data/Universe | P2 | F2 | Open | Wikipedia constituent history has bounded count drift (left-censored inflation ~3% recent era; sparse pre-2000 changes; 3 ticker-collision exclusions). |
| BUG-069 | Data/Universe | P2 | F2 | Deferred (operator-accepted 2026-07-18) | daily_signal_pipeline degrades to unfiltered provisional scores when the PIT universe import is missing/stale; no alert beyond a log warning. |
| BUG-070 | Backtesting | P1 | F2 | Open | Backtester loads a single full-history (non-cutoff-aware) adjusted price series, shared for both scoring and execution. |
| BUG-071 | Research/Signals | P2 | F2 | Open | IC-validation cutoff-aware adjustment uses one run-boundary cutoff, not a literal per-score-date cutoff (documented residual). |
| BUG-072 | Dashboard/API | P2 | F2 | Fixed | All alpha/factor-score readers (dashboards, `scripts/indicator_diagnostic.py`) now filter to the active research run by default; `--all-runs`/`--research-run-id` are the only documented explicit opt-ins for cross-run reads. |
| BUG-073 | Packaging/CI | P1 | F1 | Fixed | `pyproject.toml`'s pytest `testpaths` silently excluded ~412 tests (all of `tests/reporting/dashboards/`, `tests/infra/`) from every "full suite" run whenever a subdirectory (`tests/strategy_registry`) was also listed as its own testpath entry. |

#### Long-term / lower-risk backlog

| Bug | Category | Severity | Fix priority | Status | Short rationale |
|-----|----------|----------|--------------|--------|-----------------|
| BUG-034 | Dashboard/API | P3 | F3 | Open | Performance table formats decimal returns as percentages incorrectly. |
| BUG-035 | Dashboard/API | P3 | F3 | Open | No FastAPI/API route layer exists despite service-boundary expectations. |
| BUG-054 | Data/Storage | P3 | F3 | Open | Fundamentals backfill skip logic can leave partial ingestions stale. |
| BUG-061 | Docs/Process | P3 | F3 | Fixed | deferred_items.md RESOLVED entry used stale recent_loss_buys key after BUG-040 rename. |
| BUG-062 | Trading Safety | P3 | F3 | Fixed | Override cap validated against estimated_shares while submission uses quantity field (inconsistent). |
| BUG-063 | Packaging/CI | P3 | F3 | Fixed | __import__("json").dumps() antipattern in _write_simulation; replaced with proper import json. |

## Startup / repository state

- Current branch observed: `work`.
- No configured `origin` remote or `origin/<branch>` tracking branch was visible from `git remote -v` / branch checks at review startup.

## P0 / Critical findings

### BUG-001: Airflow paper-trading DAG cannot pass its own environment gate under Docker Compose

**Severity:** P0 / deployment blocker

**Evidence:** `docker-compose.yml` passes database, Redis, MinIO, and Polygon settings into Airflow, but does not pass `PAPER_TRADING`, `IBKR_PORT`, `IBKR_HOST`, or `IBKR_CLIENT_ID`. The paper DAG fails fast unless `PAPER_TRADING=true` and `IBKR_PORT=7497` are present. `.env.example` defines those variables, but they are not injected into the Airflow service environment.

**Impact:** The Compose-managed `daily_paper_trading` DAG will fail before performing useful work.

**Suggested direction:** Pass the IBKR/paper env vars into all Airflow containers, and add a deployment smoke test that imports the DAG and runs `_require_paper_env()` against the container environment.

**Resolution (Gate 01A, branch `dev/R2-01A-compose-runtime`):** `PAPER_TRADING`,
`IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, and `RQIS_PAPER_ARTIFACT_DIR` are
now set in `docker-compose.yml` `x-airflow-common.environment`, reaching
`airflow-init`, `airflow-webserver`, and `airflow-scheduler` identically.
Verified with `docker compose config` against `.env.example` placeholders:
`PAPER_TRADING=true`, `IBKR_PORT=7497`,
`RQIS_PAPER_ARTIFACT_DIR=/opt/airflow/rqis_paper` on all three services.
`tests/infra/test_compose_paper_runtime.py` asserts this both statically and
(when Docker is available) via a real `docker compose config` render.
Adversarial fix round (same branch): `PAPER_TRADING` and `IBKR_PORT` are
now HARD-CODED to paper values in `x-airflow-common` rather than
`.env`-substituted (a stale live-side `.env` can no longer render
`PAPER_TRADING=false`/`IBKR_PORT=7496` into the Airflow services — P2-4,
with a rendering regression test), `IBKR_CLIENT_ID` is now actually consumed
by `IBKRBroker.__init__` via a validated env default instead of being
declared-but-ignored (P1-2), and `_require_paper_env` in the DAG now also
requires `RQIS_RUNTIME_CONTEXT=compose_bridged` so a runtime that bypassed
the reviewed Compose contract fails closed at the first task (P1-1).
**Status: Fixed.** Operator-verified 2026-07-18 per
`docs/runbooks/01a_compose_paper_runtime_verification.md`: `docker compose
config` confirmed the paper-runtime variables render on all three Airflow
services against a live `.env`; `docker compose ps` showed
`airflow-webserver`/`airflow-scheduler` healthy and `airflow-init` exited
`0`. (One unrelated environment issue was hit and fixed en route: Docker Hub
had pruned the pinned `minio/mc` client tag, blocking `docker compose up`
for the whole stack; repointed to `minio/mc:latest` — see `Worklog.md`
2026-07-18.)

### BUG-002: Airflow image omits runtime dependencies used by DAG execution paths

**Severity:** P0 / deployment blocker

**Evidence:** `infra/docker/Dockerfile.airflow` installs only a short hand-picked package list, while DAG execution paths import `pandas`, `pyarrow`, `ib_insync`, database drivers, and other packages listed in `requirements.txt`.

**Impact:** The built Airflow runtime is likely to fail at task runtime with `ModuleNotFoundError`, especially for Parquet/MinIO and IBKR paper-trading paths.

**Suggested direction:** Install the project package and/or `requirements.txt` in the Airflow image, then test DAG imports inside the built image.

**Resolution (Gate 01A, branch `dev/R2-01A-compose-runtime`):**
`infra/docker/Dockerfile.airflow` now documents and installs only what is
actually missing (`ib-insync`, `minio`, `structlog`, `yfinance` -- pandas,
numpy, pyarrow, PyYAML, python-dotenv, psycopg2-binary, requests, and lxml
already ship in the base image at Airflow's own constraints versions) and
adds a build-time gate that fails the build if any Airflow-critical package
drifts from the base image or if `pip check` reports anything beyond one
documented, unused-provider mismatch (`snowflake-connector-python`
vs. `cffi`). A full `pip install --constraint <Airflow constraints>` was
tried first and rejected because it produces a genuine
`ResolutionImpossible` (yfinance's `curl_cffi` needs `cffi>=2.0`; Airflow's
constraints pin `cffi==1.16.0` for the unused snowflake provider) -- this is
recorded in the Dockerfile comments rather than masked. Python version
decision: Airflow 2.8.1 has no Python 3.12 image or constraints file
(verified directly against Docker Hub and
`constraints-2.8.1/constraints-3.12.txt`, which 404s), so the image stays on
Python 3.11; this is a documented, known gap against the project's
`requires-python>=3.12`, not an oversight.
`infra/docker/smoke_test_dag_imports.py` imports `daily_paper_trading`,
`daily_signal_pipeline`, `daily_data_pipeline`, and every module reached
through the C1 approval gate inside the built image and asserts
`airflow.__file__` resolves to the installed package, not this repo's
`airflow/` test-stub. `tests/infra/test_airflow_image_smoke.py` wires a full
`docker build` + smoke-run into pytest (skips without Docker). Verified
locally end-to-end with Docker Desktop 29.6.1 / Compose v5.2.0: build
succeeds, `airflow version` reports `2.8.1`, and all 20 target modules
import with exit 0. Adversarial fix round (same branch): the build-time
`pip check` gate now also fails on an abnormal pip exit code or unexpected
stderr output (previously a pip internal error with empty stdout would have
passed silently — P2-1), and the allowlist anchors the full expected
snowflake/cffi complaint text rather than a bare package-name prefix
(P3). **Status: Fixed.** Operator-verified 2026-07-18: `docker compose build`
succeeded against a live `.env` on Windows Docker Desktop 29.6.1, and
`airflow-scheduler`/`airflow-webserver` came up healthy from the built image
(see `docs/runbooks/01a_compose_paper_runtime_verification.md`).

### BUG-003: Paper-trading artifacts are written to an unmounted container path

**Severity:** P0 / approval workflow blocker

**Evidence:** The paper DAG defaults artifacts to `/opt/airflow/rqis_paper` and documents it as a shared Docker Compose volume. Compose does not mount that path into Airflow containers or the dashboard.

**Impact:** Blotter artifacts can be invisible to host-side approval tools/dashboards and may disappear on container recreation.

**Suggested direction:** Add a named volume or host bind mount for `RQIS_PAPER_ARTIFACT_DIR` shared by Airflow and the dashboard/approval tooling.

**Resolution (Gate 01A, branch `dev/R2-01A-compose-runtime`):** confirmed
there is no Compose dashboard service (the Streamlit dashboard is launched
host-side via `streamlit run reporting/dashboards/app.py`), so
`docker-compose.yml` now bind-mounts
`${RQIS_PAPER_ARTIFACT_HOST_DIR:-./local/paper_artifacts}` at the identical
in-container path `/opt/airflow/rqis_paper` on every Airflow service, and
`.env.example` documents the corresponding host-side
`RQIS_PAPER_ARTIFACT_DIR` value the dashboard process should export. The
`airflow-init` startup command now creates the directory and performs a
write-permission check before `airflow db migrate`.
`tests/infra/test_paper_artifact_shared_storage.py` proves a container
write (as uid 50000, the base image's non-root `airflow` user) is
host-readable with a matching SHA-256, and that all three Airflow services
mount the same path from the same env-configurable source. Adversarial fix
round (same branch): the Docker smoke test's mount list is now derived from
`docker-compose.yml` `x-airflow-common.volumes` instead of being duplicated
by hand, with a consistency test that fails if a compose mount the DAG
runtime needs is removed (P2-3). **Status: Fixed.** Operator-verified
2026-07-18: a sentinel written from inside `airflow-scheduler` to
`$RQIS_PAPER_ARTIFACT_DIR` was read back byte-identical from the host bind
mount both before and after an `airflow-scheduler` container restart.

### BUG-004: IBKR connectivity defaults to container-local localhost

**Severity:** P0 / broker connectivity blocker

**Evidence:** `IBKRBroker` defaults to `IBKR_HOST=127.0.0.1`; `.env.example` does the same; the DAG constructs `IBKRBroker()` without a host. In Docker, `127.0.0.1` is the container, not the host running TWS/IB Gateway.

**Impact:** Even with env vars passed, containerized paper trading will usually fail to reach the broker socket.

**Suggested direction:** Document and configure a Docker-safe host (`host.docker.internal` plus Linux `extra_hosts`, host networking, or explicit gateway IP), and require a connectivity preflight in containerized runs.

**Resolution (Gate 01A, branch `dev/R2-01A-compose-runtime`):** added a
distinct `IBKR_HOST_AIRFLOW` `.env` variable (default `host.docker.internal`)
feeding the Airflow containers' `IBKR_HOST`, kept separate from the
host-side `IBKR_HOST=127.0.0.1` used by operator CLI scripts, plus a
portable `extra_hosts: ["host.docker.internal:host-gateway"]` mapping for
Linux Docker Engine compatibility (Windows Docker Desktop targeted now per
the plan's open decision 3; Linux untested). More importantly, added a
production (not test-only) fail-closed guard,
`execution/brokers/ibkr.py._validate_bridged_broker_host()`, called from
both `IBKRBroker.__init__` and `connect()`: when
`RQIS_RUNTIME_CONTEXT=compose_bridged` is set (every Airflow Compose
service sets it), an unset/empty/`localhost`/`127.0.0.1`/`::1`/`0.0.0.0`
`IBKR_HOST` raises `OSError` before any connection attempt, with a declared
`RQIS_RUNTIME_NETWORK_MODE=host` escape hatch for a genuinely
host-networked deployment. `execution/tests/test_ibkr_bridged_host_validation.py`
and `execution/tests/test_ibkr_broker_endpoint_fail_closed.py` cover
unset/empty/loopback rejection, the live-port-7496 failure path, and an
unresolvable-host `connect()` failure propagating out of the DAG's
`_fetch_ibkr_snapshot` task (the first broker-touching task) without being
swallowed. Adversarial fix round (same branch): the guard now treats ANY
non-empty `RQIS_RUNTIME_CONTEXT` value as containerized fail-closed (a typo
like `compose-bridged` can no longer silently deactivate enforcement —
P2-2), and the DAG's `_require_paper_env` requires the marker outright so
the guard cannot be bypassed by omitting it (P1-1).
**Status: Fixed.** Operator-verified 2026-07-18: `python -m
scripts.paper_readiness_check` run from inside `airflow-scheduler` reported
`OK: TWS/Gateway socket reachable at host.docker.internal:7497` and `OK:
IBKRBroker connected in paper mode` against a live TWS paper session,
proving the container correctly reaches the host broker through
`host.docker.internal` rather than falling back to a container-local
address. (The script then failed a downstream, unrelated CAD/USD FX-rate
fetch because the paper account lacks a live IBKR FX market-data
subscription — a pre-existing account limitation with an existing
`IBKR_FX_RATE_CAD_USD`/`_AS_OF` manual fallback documented in
`CLAUDE.md`/`.env.example`, not a BUG-004 defect.)

### BUG-005: Approval quantity overrides can be tampered upward and bypass validation

**Severity:** Critical / trading safety

**Evidence:** The dashboard caps operator quantity edits, but Airflow later trusts `quantity_overrides` from the approval row/XCom and applies them directly. The quantity validator checks `estimated_shares`, while order construction submits `quantity` when present.

**Impact:** A malicious or erroneous DB row such as `quantity_overrides={"1": 1000000}` can cause an oversized broker order to be submitted while the existing validator still passes on original `estimated_shares`.

**Suggested direction:** Validate each override server-side immediately before submission: integer, finite, positive, less than or equal to the original approved share count, selected order only, and re-run notional/risk checks on the overridden rows.

### BUG-006: Corrupt partial reconciliation artifacts fail open and can duplicate broker orders

**Severity:** Critical / trading safety

**Evidence:** On retry, `_submit_orders` attempts to read an existing reconciliation artifact to skip previously submitted sequence IDs. If the artifact exists but is corrupt/unreadable, the code logs and continues with an empty `already_submitted_seqs`; later it can also reset `previous_responses` to `[]`.

**Impact:** A partial write, truncation, or manual edit can cause the retry path to resubmit orders already accepted by IBKR.

**Suggested direction:** Fail closed on corrupt reconciliation artifacts and require manual broker reconciliation or broker-backed idempotency proof before retrying any submission.

### BUG-007: Risk dashboard can silently report zero/incorrect risk due to position schema mismatch

**Severity:** P0 / risk monitoring correctness

**Evidence:** The paper DAG writes positions with keys including `price`; the Risk page builds its price map from `current_price` only. When positions only have `price`, they are excluded from risk weights. The risk monitor then treats empty weights as zero concentration/beta exposure rather than a hard data-quality failure.

**Impact:** A real portfolio can appear to have no concentration/beta risk, and circuit-breaker display/alerts may not reflect actual holdings.

**Suggested direction:** Standardize the portfolio snapshot schema, accept the producer’s price field or migrate producers/consumers together, and fail closed when positions cannot be priced.

### BUG-008: Current-membership universe creates survivorship/data leakage in IC research

**Severity:** P0 / research validity

**Status:** Fixed. Merged to `dev/R2-phase1` via PR #34 (`1df242e`), branch
`dev/R2-01B2-pit-universe` (roadmap item 01B-2, scoped to
`docs/plans/01b-research-validity-design.md` §1). Review history: one internal
adversarial round plus six Codex review rounds (seven items total, including
two P1s beyond the adversarial round's own findings — SQLAlchemy 1.4
compatibility for the DAG's PIT filter, and PIT membership applied before
cross-sectional z-scoring, closed as a class sweep across all four factor
composites — and a final P2 on the quality factor's independent fundamentals
load bypassing that same eligibility filter). 1608+ tests passing at merge.
Delivered: Alembic migration 009 (effective-dated `universe_membership`,
`universe_symbol_history`, `universe_import_batches`); provider-agnostic import
pipeline with checksummed raw-source persistence and publish-only-validated gates
(`data/universe/import_pipeline.py`); real Wikipedia constituent history imported
and verified from the checked-in snapshot `data/vendor/wikipedia_sp500/2026-07-17/`
(source contract: `docs/plans/01b2-constituent-source-contract.md`); fail-closed
runtime API `data/universe/runtime.py` (`load_universe_as_of` / `PITUniverseLookup`)
with type-level rejection of current-universe objects in historical code;
`compute_ic_series`, `scripts/validate_signal_ic.py`, and
`scripts/backfill_momentum_scores.py` migrated to PIT membership enforcement;
operational current-mode callers explicitly labeled. All §1.4 acceptance tests
pass (`data/tests/universe/test_acceptance_1_4.py`). Historical outputs computed
under the old current-membership universe remain provisional; recompute is 01B-3/§4
scope. Residual data-quality limits tracked as BUG-068/BUG-069.

Adversarial-review fix round (same branch): operator `--exclude-tickers`
exclusions are now persisted as a JSON audit record on
`universe_import_batches.excluded_tickers` (migration 009 amended pre-release)
and surfaced by `coverage_report()`; migration 010 adds an interim
`signal_ic_stats.provisional` boolean (existing rows default TRUE — all
pre-01B-2 rows are provisional by definition; superseded by 01B-3's §4
`research_run_id` identity) stamped by `scripts/validate_signal_ic.py`;
the Wikipedia changes-table parser validates actual header text before its
positional rename and fails closed on reordered/renamed headers; left-censored
intervals get `known_at` at the coverage-start session close so pre-window
members are eligible on day one of the certified window; the survivorship
warning in `validate_signal_ic.py` is gated to provisional runs; snapshot
checksum-tamper detection is now covered by tests.

**Evidence:** The research universe is documented as current-membership S&P 500 and excludes removed constituents. The IC engine merges scores to forward returns on available ticker/date rows without enforcing point-in-time membership.

**Impact:** Historical IC can be biased upward by excluding bankrupt, removed, or underperforming names that were in the investable universe at the time.

**Suggested direction:** Add a PIT constituent/membership table and require IC computations to filter by membership as of each signal date.

### BUG-009: Same-close signal/return timing can introduce lookahead

**Severity:** P0 / research and backtest validity

**Evidence:** Forward returns are computed from signal-date close to future close. Many price signals use the signal-date close in the signal itself.

**Impact:** Unless the project explicitly assumes known-before-close signals and close/auction execution, IC/backtests can include a one-bar lookahead.

**Suggested direction:** Shift signals or forward-return windows to the next executable bar, or enforce timestamped market-on-close assumptions.

**Status:** In review. Implemented on branch `dev/R2-01B3-timing-contract`
(roadmap item 01B-3, scoped to `docs/plans/01b-research-validity-design.md`
§2 and §4), not yet merged. Delivered: `signals/research/timing.py`
(`TimingPolicy`, `build_return_series`, `reject_same_date`) enforcing the
baseline `score_date < entry_date < exit_date` (t+1 close) convention on each
ticker's own trading calendar; `signals/research/ic.py`
`compute_forward_returns`/`compute_ic_series` rewritten to delegate to it,
name every date explicitly (`score_date`/`entry_date`/`exit_date` — never a
bare `date`), and check PIT membership on all three dates, not just
`score_date`; two explicitly named corporate-action interfaces in
`data/normalization/corporate_actions.py`
(`build_score_price_history_as_of`, `build_realized_total_return_as_of`)
backed by a new `known_at`/`announced_at`/`source_version` availability
contract on `corporate_actions` (migration 011, conservative next-session
backfill for legacy yfinance rows); versioned research identity
(`research_methodologies`/`research_runs`, migration 012) with a
`research_run_id` FK now part of the unique constraint/primary key on
`signal_ic_stats`/`factor_scores`/`alpha_scores` so a new run cannot silently
UPSERT over an old methodology's rows; `scripts/validate_signal_ic.py` and
`scripts/audit_pit_safety.py` updated accordingly (the audit script now also
empirically verifies entry/exit alignment on live price data, not just
`score_date < sim_date`).

**Adversarial review fix round (same day):** the first pass wired the
timing-contract fix into `signals/research/ic.py` but left the two new
cutoff-aware corporate-action builders with no production caller — the live
`scripts/validate_signal_ic.py`/`scripts/backfill_momentum_scores.py` still
loaded raw `close` with zero adjustment (worse than the pre-existing
full-history routine). Both scripts now call `build_score_price_history_as_of`
(momentum/lowvol — price-ratio-based factors) and
`build_realized_total_return_as_of` (the forward/realized-return leg for
every factor) before prices reach scoring/`compute_ic_series` — see BUG-071
for the documented residual (single run-boundary cutoff, not literal
per-score-date). Also fixed: `airflow/dags/daily_signal_pipeline.py` would
have hard-failed its first post-migration run (no active research run was
ever registered for the methodology name it requires) — see
`scripts/register_operational_research_run.py` and the pre-deploy blocker
note below. `data/normalization/corporate_actions.py`'s cutoff filter now
also requires an action to have occurred (`ex_date <= cutoff`), not just been
announced, matching `build_realized_total_return_as_of`'s docstring.

Full non-tearsheet repo suite passing at review time (1600+ tests). No
historical scores/backtests were recomputed by this change (identity/
invalidation machinery only, per the design plan's implementation order).

**PRE-MERGE/PRE-DEPLOY BLOCKER:** before migration 012 is applied to any
database `daily_signal_pipeline.py` runs against, an operator MUST run
`python -m scripts.register_operational_research_run` once (idempotent) to
register and activate the `daily_signal_pipeline_operational` methodology/
run — otherwise the next scheduled DAG run (`30 21 * * 1-5`) fails closed
with `NoActiveResearchRunError`. See
`docs/runbooks/research_run_registration.md`.

**Adversarial review round 2 (same day):** two more confirmed findings.
`airflow/dags/daily_signal_pipeline.py` never got the corporate-action
adjustment wiring the CLI scripts received — `_load_prices` now also loads
`corporate_actions` and calls `build_score_price_history_as_of` with
`score_cutoff = session_close_cutoff(end_date)` (this DAG scores exactly one
date per run, so this is the exact per-date cutoff, no BUG-071-style
approximation) before `_compute_momentum`/`_compute_lowvol` run; a new
`adjusted_prices_json` XCom key carries the adjusted series,
`_compute_value`/`_compute_quality` keep the raw `prices_json` key. Fixing
this surfaced a second, unrelated latent bug: `_write_scores`'s active-run
lookup imported `data.research.identity`, which imports SQLAlchemy-2-only
ORM APIs the packaged Airflow image (SQLAlchemy 1.4.51 pinned, see
`infra/docker/Dockerfile.airflow`) cannot import — replaced with a plain-SQL
`_get_active_research_run_id_sql`, kept in semantic lockstep via new parity
tests, and the DAG's existing import-isolation test now bans that import
path. Also fixed: migration 011's `sys.path` shim resolved to `infra/`, not
the repo root (`parents[3]` → `parents[4]`, with an assertion guard).

**Adversarial review round 3 (same day):** research_run_id is now part of
`alpha_scores`/`factor_scores` identity, so multiple rows can legitimately
coexist for the same `(ticker, score_date, strategy_id)` across runs
(legacy, superseded, active) — every production reader must filter to the
active run explicitly, never read across all of them. Fixed:
`scripts/paper_inputs_check.py::_load_latest_scores` (and, through it,
`paper_target_check.py`/`paper_order_candidates_check.py`/
`paper_risk_compliance_check.py`/`paper_stage_blotter_check.py`, all of
which import it) now resolves and filters to the active
`daily_signal_pipeline_operational` run, failing closed with an actionable
message if none is active; `daily_signal_pipeline.py`'s `_write_simulation`
fallback query (for strategies not present in the current run's own XCom)
now filters the same way, degrading (skip, not crash) if no active run can
be resolved; `scripts/pin_snapshot.py` gained a `--research-run-id` option
and detects+rejects (rather than silently pinning) the case where more than
one run's rows collide on the same `(ticker, score_date)`. Also fixed (P2):
`scripts/backfill_momentum_scores.py` raised a bare `KeyError` against a
`corporate_actions` snapshot pinned before migration 011 (no
`known_at`/`source_version` columns); it now synthesizes `known_at` via the
same conservative next-session rule migration 011 used, tagging synthesized
rows with a distinct `source_version` so they remain distinguishable from a
genuinely migrated live-table read. See BUG-072 for the one reader class
(Streamlit dashboard queries, `scripts/indicator_diagnostic.py`) explicitly
scoped OUT of this round with rationale, rather than silently left
unaddressed.

**Adversarial review round 4 (same day) — the most serious finding of this
whole review series:** the single-run-boundary-cutoff simplification
documented as BUG-071 was safe for the SCORE series but NOT for the
REALIZED-RETURN series: for an earlier exit date in a holdout, an action
whose `ex_date` fell between that specific entry/exit pair but whose
`known_at` was after that exit (yet before the later shared boundary) was
incorrectly included — future information leaking into a persisted,
"PIT-safe" IC result, for every exit date in a holdout except the very
last one. This is fixed, not approximated: new
`signals.research.ic.compute_realized_forward_returns_as_of` builds a
genuinely per-exit-date-cutoff-correct realized-return series (one
`build_realized_total_return_as_of` call per distinct exit date needed,
not one shared boundary), and `compute_ic_series` gained a
`precomputed_forward_returns` parameter so `scripts/validate_signal_ic.py`
can feed it in directly, bypassing the same-series shortcut entirely for
the realized-return leg. `_build_adjusted_price_series` was split into
`_build_score_adjusted_prices` (score leg only, single boundary cutoff —
re-verified safe for a structurally different reason than the
realized-return case, see BUG-071's updated entry) plus the new realized-
return path. A new test (`TestComputeRealizedForwardReturnsAsOf::
test_action_unknown_at_earlier_exit_does_not_adjust_it`) reproduces the
exact leak scenario and proves the earlier exit's return is unaffected by
an action not yet known at ITS cutoff, while a later exit whose own cutoff
does cover it is adjusted correctly.

Also fixed the same round: `scripts/backfill_momentum_scores.py`'s
`--research-run-id` CLI flag is no longer `required=True` (it blocked the
documented `--dry-run` preview command, which never persists and never
needed it — `run()` still hard-requires it for an actual write); and
`reporting/dashboards/queries.py`'s `latest_alpha_scores`/
`bottom_alpha_scores`/`factor_scores_for_ticker` now filter to the active
research run (closing the `queries.py` portion of BUG-072 — see that
entry for what remains open there).

**Adversarial review round 5 (same day):** the round-2 fix (plain-SQL
active-run lookup, avoiding the SQLAlchemy-2-only ORM in
`data.research.identity`/`data.research.models` because the packaged
Airflow image pins SQLAlchemy 1.4.51) was reintroduced as a regression at a
SECOND call site: round 3's `scripts/paper_inputs_check.py::
_resolve_active_research_run_id` fix used the ORM `get_active_research_run`
directly, and `paper_inputs_check.py` is Airflow-reachable via
`airflow/dags/daily_paper_trading.py`'s `_verify_inputs`/`_construct_target`
tasks — the identical failure mode as round 2, just a different file. Fixed
by extracting the lookup into a new shared module,
`data/research/sql_compat.py` (plain `text()` SQL, zero ORM imports),
that both `airflow/dags/daily_signal_pipeline.py::
_get_active_research_run_id_sql` and `scripts/paper_inputs_check.py::
_resolve_active_research_run_id` now delegate to, so the lookup exists in
exactly one place instead of being reimplemented (or regressed) at each
new call site. Repo-wide grep confirmed no other Airflow-reachable call
site had the same regression. Added
`tests/test_airflow_sqlalchemy_import_isolation.py`: a generic,
repo-wide (not DAG-specific) static check that walks every DAG under
`airflow/dags/` for `scripts.*` imports (module-level or lazy, anywhere in
the file) and asserts neither the DAG nor any transitively-imported script
imports the banned SQLAlchemy-2-only modules — so this class of bug fails
a fast local test instead of recurring silently a third time.

Also fixed (P2): `compute_ic_series`'s `precomputed_forward_returns` mode
previously stamped output rows with the function's own `timing_policy`
argument rather than the `timing_policy_id` actually present in the
supplied frame's rows — mislabeling the persisted research identity for
any caller building a frame under a non-default policy, or silently
picking one policy if the frame mixed several. Now reads the frame's own
column (rejecting with a clear error if it contains more than one distinct
value) instead of trusting the argument.

**Adversarial review round 6 (same day):** `reporting/dashboards/
queries.py::pipeline_health()`'s signals-recency check ran
`MAX(computed_at)` over ALL of `alpha_scores` with no active-run filter —
the one dashboard query round 4's `_ACTIVE_RUN_SUBQUERY` fix missed. A
fresher row left behind by an inactive/superseded run (post-backfill or
run rotation) could make the dashboard report the pipeline healthy while
the active `daily_signal_pipeline_operational` run was actually stale or
had never written anything, undermining the active-run invariant this PR
spent five rounds establishing everywhere else. Fixed with the same
`_ACTIVE_RUN_SUBQUERY` pattern already used by the other three dashboard
queries in this file (kept for in-module consistency rather than switching
to `data/research/sql_compat.py`, which raises on no-active-run rather
than degrading to an empty/None result the way this file's existing
pattern does). New `tests/reporting/dashboards/test_pipeline_health.py`
(the function had zero prior test coverage) reproduces the exact scenario
Codex described and proves the active run's own staleness now wins over a
fresher inactive run's row. Also hardened `_age()` to accept a string
timestamp defensively (a SQLite/pysqlite `MAX()`-over-aggregate quirk that
loses the declared column type, surfaced only by adding real test
coverage — the production Postgres driver was never affected).

**Adversarial review round 7 (same day):** three P2 findings, closing out
this review series. (1) The remaining two dashboard readers,
`reporting/dashboards/queries.py`'s `alpha_score_at_fill_date`/
`factor_scores_at_fill_date` and `reporting/dashboards/simulation.py`'s
`alpha_overlap_matrix`, were fixed with the same `_ACTIVE_RUN_SUBQUERY`
pattern (see BUG-072, now closed for every reader except the deliberately
deferred `scripts/indicator_diagnostic.py`); a repo-wide grep for every
remaining `FROM alpha_scores`/`FROM factor_scores` confirmed nothing else
is unfiltered without a documented reason. (2)
`scripts/register_operational_research_run.py` registered
`action_source_version="yfinance-current"`, but ingestion
(`data/ingestion/market/yfinance_client.py`,
`airflow/dags/daily_data_pipeline.py`) calls
`TimescaleWriter.upsert_corporate_actions(df)` without a `source_version`
argument, so every row actually lands with the writer's own default,
`"unknown"` — the registered methodology's provenance claim didn't match
reality, undermining exactly what migrations 011/012 exist to guarantee.
Fixed by registering `"unknown"` (matching actual behavior) rather than
wiring a real version through ingestion (out of scope for this fix — that
touches production ingestion code, not just methodology metadata); the
`notes` field now documents the gap and the tightening path (re-register a
NEW methodology once ingestion passes a real version, never edit this one
in place). (3) `signals.research.ic.compute_realized_forward_returns_as_of`
(the round-4 correctness fix) rebuilt a full adjusted price panel per
DISTINCT EXIT DATE — assessed as a real but cheaply fixable performance
concern, not deferred: it now caches the expensive panel build keyed by
the actual SET of eligible corporate actions for a cutoff (reusing
`_filter_actions_by_cutoff` directly so the cache key can never drift from
what the builder itself considers eligible), since many consecutive exit
dates in a multi-year holdout (~500-750 distinct trading dates) commonly
share an identical eligible-action set — nothing new becomes knowable
between them — collapsing what was an O(#exit_dates) loop of full-panel
rebuilds to O(#distinct eligible-action-set changes), which is bounded by
the number of corporate actions (typically dozens, far fewer than trading
dates) rather than the holdout length. This is a pure performance
optimization — the cache key is exactly the input that determines the
adjusted panel's content, so it cannot change correctness — proven by new
tests (`test_consecutive_exit_dates_with_no_new_actions_share_one_panel_build`,
`test_new_action_forces_a_fresh_panel_build`) that count the underlying
builder calls directly, plus the existing round-4 leak-reproduction test
continuing to pass unchanged.

**Adversarial review round 8 (same day):** one P1, one P2. (1) **P1:**
`scripts/register_operational_research_run.py`'s `ensure_operational_run()`
returned the live `ResearchMethodology` ORM instance, but `main()` read
`methodology.id`/`.name` from it AFTER the `with Session(engine) as
session:` block had already closed the session. Because
`session.commit()` expires ORM instances by default
(`expire_on_commit=True`), that post-close attribute access re-queried
through a closed session and raised `DetachedInstanceError` —
`main()`'s very first successful run would crash on its own final print
statement, the exact "P1-fix-for-a-P1-fix" scenario this script exists to
prevent (this script was built in round 3 specifically so
`daily_signal_pipeline.py` doesn't fail closed on its first post-migration-
012 run). Confirmed by running the actual script end-to-end against a real
(SQLite) engine, not just the mocked-session unit tests, which had kept the
session open across every assertion and so never exercised the close
boundary that trips this bug. Fixed: `ensure_operational_run()` now reads
`.id`/`.name` while the session is still open and returns plain scalars
(`methodology_id: int, methodology_name: str, run_id: int, created: bool`)
instead of the ORM object — the caller can no longer touch an
expired/detached instance because it never receives one. New regression
test `test_result_survives_session_close_like_main_does` in
`tests/test_register_operational_research_run.py` deliberately mirrors
`main()`'s exact open-then-close pattern (unpacks the result only after the
`with Session(...)` block exits) rather than asserting inside it, so it
would have caught this before it shipped. (2) **P2, closing the last open
item of BUG-072:** `scripts/indicator_diagnostic.py::_load_factor_scores`
loaded `factor_scores` by `strategy_id`/date range only, with no
`research_run_id` filter — round 7 had left this open as a "deliberate,
documented" research/diagnostic-tool exception, but Codex made the fair
point that migration 012's widened PK (adding `research_run_id`) makes this
a real correctness risk for the tool's own stated purpose, not just
staleness: the tool's own duplicate-row detector only warns, and
`pivot_table` silently averages duplicate `(ticker, score_date,
factor_name)` rows, so a mixed-run blend could reach the reliability/
validity report undetected. Fixed by defaulting to the same
`_ACTIVE_RUN_SUBQUERY` pattern used everywhere else under BUG-072, with a
new `--all-runs` explicit opt-in that itself fails closed (raises) on a
real cross-run collision rather than silently blending. See BUG-072 for the
full writeup — that entry is now fully closed.

**Adversarial review round 9 (same day):** one P1, one P2. (1) **P1:**
`scripts/backfill_momentum_scores.py` silently degraded to raw (unadjusted)
prices when no `corporate_actions` snapshot was pinned for
`--snapshot-date`, and LIVE (non-dry-run) writes proceeded anyway —
persisting momentum scores tagged with a `research_run_id` whose
registered methodology (`score_cutoff_known_at_v1`) falsely claims
cutoff-adjusted corporate-action handling was applied. The same
"provenance lies about what actually happened" pattern as the original P0
finding this whole task exists to prevent, reached this time via a silent
degrade instead of missing wiring. Fixed: a live write now fails closed by
default on a missing snapshot; `--dry-run` stays permissive (preview only,
never persists — no persisted provenance to lie about). A new
`--allow-raw-prices-on-missing-actions` opt-in requires `--research-run-id`
and a new `_validate_raw_prices_methodology_is_honest` helper queries that
run's registered methodology, refusing to proceed if
`score_action_availability_policy == "score_cutoff_known_at_v1"` — the
operator must register and pass a run under a methodology that honestly
declares no cutoff adjustment was applied. (This script is not
Airflow-reachable, so using the ORM here is safe, unlike the DAG-reachable
modules elsewhere in this bug.) New tests
(`data/tests/universe/test_backfill_universe_filter.py::
TestMissingActionsFailsClosedOnLiveWrite`,
`TestValidateRawPricesMethodologyIsHonest`) cover: live write without
opt-in raises before any DB write, opt-in without `--research-run-id`
raises, opt-in with a dishonest methodology raises, dry-run stays
permissive, and the honesty gate is unit tested directly. (2) **P2:** the
`precomputed_forward_returns` bypass in `compute_ic_series`
(`signals/research/ic.py`, added round 4) validated columns before use but
never checked `score_date < entry_date < exit_date` on the supplied rows
— it deliberately skips the internal `build_return_series` call where
that invariant is normally enforced, making it the one entry point that
could silently reintroduce the exact BUG-009 lookahead. Fixed: new
`_validate_return_series_date_ordering` checks every distinct date triple
in the frame (deduped across tickers sharing a cross-section), reusing
`signals.research.timing.reject_same_date` for the score/entry pair and
raising the same `SameDateScoreError` for entry/exit. Both entry points to
`compute_ic_series` now enforce the identical invariant. New tests in
`signals/tests/test_ic.py` reproduce a same-close precomputed frame being
rejected, an exit-before-entry frame being rejected, and confirm a
genuinely valid hand-built frame still passes.

The PM independently reproduced and confirmed round 8's BUG-073 testpaths
finding this round (old buggy config: 1685 vs corrected 2096, previously
hidden subtrees run clean) — no further action needed there.

**Adversarial review round 10 (same day):** two P2s, plus a required
self-audit sweep before closing the round. (1) **P2:**
`scripts/audit_pit_safety.py::_empirical_audit` recomputed expected
momentum from raw `daily_prices.close` with no corporate-action
adjustment, but the backfill (round 4/9) writes scores from a
cutoff-adjusted `adj_close` series — any audited window crossing a
split/dividend showed a spurious mismatch, making the audit tool itself
unreliable exactly where it matters most. Fixed: new
`_adjusted_prices_for_audit` rebuilds the same cutoff-adjusted scoring
input the backfill actually wrote scores from (reusing
`build_score_price_history_as_of`), with an optional
`--corporate-actions-file`/automatic snapshot load that degrades to a
loud raw-price caveat (not a failure — this is a read-only diagnostic
tool) if unavailable. New test
`test_audit_empirical_false_positives_without_corporate_actions_but_clean_with_them`
reproduces the exact finding: raw recomputation false-positives across a
split window; supplying `corporate_actions` reports zero violations. (2)
**P2:** `--provisional-no-universe` on `scripts/backfill_momentum_scores.py`
could be combined with a live write tagged to the ACTIVE operational
`research_run_id` (whose methodology claims PIT-universe safety), letting
current-membership (survivorship-biased) rows masquerade as PIT-safe to
any downstream reader filtering solely by `research_run_id` (BUG-072) —
same honesty-check pattern as round 9, on the universe dimension instead
of the corporate-action dimension. Fixed: new
`_validate_provisional_no_universe_methodology_is_honest` (sharing the
plain-scalar `_get_methodology_fields_for_run` lookup with round 9's
check) refuses a live `--provisional-no-universe` write unless
`--research-run-id` points at a methodology that honestly declares no PIT
filtering was applied. New tests in
`data/tests/universe/test_backfill_universe_filter.py::
TestProvisionalNoUniverseFailsClosedOnLiveWrite`/
`TestValidateProvisionalNoUniverseMethodologyIsHonest` cover the gate and
its unit-level honesty check directly; the round-9 tests that had used
`--provisional-no-universe` merely as a convenience were updated to use
the PIT `lookup` fixture instead, since the new round-10 gate would
otherwise intercept them before they reached what they were actually
testing.

**Round-10 self-audit sweep (required before closing the round):** a
comprehensive repo-wide check across four dimensions, per explicit review
directive.
- *Corporate-action adjustment wiring*: grepped every production path
  reading `daily_prices`/computing a return. Found and fixed a THIRD
  instance of the same defect class:
  `airflow/dags/daily_signal_pipeline.py::_write_simulation`'s
  close-to-close daily return divided RAW prices with no adjustment — a
  split/dividend on `sim_date` or `prev_date` would inject a fabricated
  return into the COMPOUNDING `simulated_nav` chain, permanently
  distorting every later NAV row for the strategy. Fixed via new
  `_adjusted_closes_for_simulation`, reusing
  `build_realized_total_return_as_of` with `entry_date=prev_date` and
  `exit_cutoff=session_close_cutoff(sim_date)` (exact per-run cutoff, not
  an approximation, since this task scores exactly one `sim_date` per DAG
  run); non-blocking degrade-to-raw on any adjustment failure
  (`strategy_simulations` has no `research_run_id` column, so there is no
  provenance-honesty claim to violate by degrading). Extracted as a
  standalone testable function since the production INSERT uses
  Postgres-only `jsonb` CAST syntax SQLite tests can't exercise
  end-to-end. New tests in `tests/test_daily_signal_pipeline_pit.py::
  TestSimulationCorporateActionAdjustment` prove a split is correctly
  back-adjusted (raw closes would imply a fake ~49% loss; adjusted is a
  genuine +2% gain), and that missing/absent corporate actions degrade to
  raw without raising. `signals/composites/momentum_score.py`/
  `low_vol_score.py` are pure functions operating on whatever series their
  caller passes and were confirmed to always be fed already-adjusted
  series by every caller checked.
- *SQLAlchemy 1.4/2.0 isolation test robustness*: audited
  `tests/test_airflow_sqlalchemy_import_isolation.py` itself and found it
  only walked ONE level deep (the DAG plus its direct `scripts.*`
  imports), not the full transitive closure — a script reachable ONLY
  through another script (a real, multi-level import graph exists in the
  `scripts/paper_*_check.py` family) would have been silently unchecked,
  and `data.*` modules reached only through a script were never checked at
  all. Rewritten to a proper BFS closure over every `scripts.*`/`data.*`
  module transitively reachable from each DAG, with a sanity assertion
  that the BFS actually reaches depth > 1 for `daily_paper_trading.py` (so
  a future regression narrowing this back to depth-1 fails the test rather
  than silently passing). No live gap found — the repo's actual import
  graph is currently clean — but the coverage gap in the guard itself was
  real.
- *Provenance-honesty sweep*: grepped every `research_run_id=`/
  `research_run_id[...]=` write-site repo-wide. Found and fixed a second
  instance of the round-10 universe-honesty gap:
  `scripts/validate_signal_ic.py`'s `--persist` path (writes
  `signal_ic_stats`) had the identical `--provisional-no-universe` +
  `--research-run-id` risk as `backfill_momentum_scores.py` — its per-row
  `provisional` column (migration 010) self-describes honestly, but
  migration 012's docstring is explicit that `research_runs.status`/
  `is_active` (reached via `research_run_id`), not `provisional`, is now
  the authoritative marker, so a reader trusting that newer model could
  still be misled by a PIT-claiming methodology tag. Fixed with the same
  `_validate_provisional_no_universe_methodology_is_honest` pattern
  (duplicated deliberately rather than refactored into a shared module,
  to avoid touching already-reviewed round-9/round-10 code under review
  pressure), wired into `main()` alongside the existing
  `--persist`/`--research-run-id` requirement check. `airflow/dags/
  daily_signal_pipeline.py::_write_scores` and `scripts/pin_snapshot.py`
  were reviewed and found exempt by design (the former resolves
  `research_run_id` via the DAG's own fixed active-run lookup with no
  operator-suppliable override; the latter already has its own explicit
  `--research-run-id` opt-in plus same-natural-key collision detection,
  round 3). `data/storage/timescale_writer.py`'s `upsert_factor_scores`/
  `upsert_alpha_scores` are infra-layer writers that require
  `research_run_id` present but have no methodology-claim semantics of
  their own — the honesty check is each higher-level caller's
  responsibility, already covered at every current call site.
- *Active-run read-filtering final sweep*: repeated the BUG-072 grep for
  every `FROM alpha_scores`/`FROM factor_scores`/ORM-table-reflection
  read across the full repo (including `notebooks/`, `mcp_servers/`,
  which currently have no matching code). Confirmed every reader is
  either filtered via `_ACTIVE_RUN_SUBQUERY`/an explicit `research_run_id`
  parameter, or is one of the two documented exceptions
  (`scripts/pin_snapshot.py`, `scripts/indicator_diagnostic.py`'s
  `--all-runs` opt-in). No new gap found.

### BUG-010: `pct_change()` missing-data defaults distort many indicators

**Severity:** P0 / signal correctness

**Status:** Fixed. Merged to `dev/R2-phase1` via PR #32 (`65e1b72`), branch
`dev/R2-01B1-missing-data` (roadmap item 01B-1, scoped to
`docs/plans/01b-research-validity-design.md` §3).

**Evidence:** Multiple indicators call `pct_change()` without `fill_method=None` on wide data containing NaNs.

**Impact:** Pandas can forward-fill missing prices before return calculations, creating artificial zero returns, suppressed volatility/beta, and distorted volume-price signs.

**Suggested direction:** Use `pct_change(fill_method=None)` consistently and require sufficient non-null observations per ticker/window.

**Fix summary:** Inventoried and migrated all 33 production `pct_change()` call sites
(`docs/plans/01b1-pct-change-inventory.md`) across `signals/indicators/*` (momentum,
volume, volatility), `backtesting/engine/{data_handler,event_loop}.py`,
`portfolio/risk_model/covariance.py`, and `reporting/dashboards/{queries.py,
pages/5_Performance.py}`. Added `signals.indicators._price_utils.daily_return`
(`pct_change(fill_method=None)` + positive-finite price validation),
`rolling_valid_count`/`require_full_window` for cumsum/mask-based indicators that
don't propagate NaN through arithmetic (`volume_up_down_ratio_21d`,
`obv_momentum_21d/63d`, `price_volume_trend_21d`), and raised every return-derived
rolling `min_periods` to its full window so a gap suppresses the value by default
(one documented exception: `vol_trend_slope_63d`'s outer OLS trend fit, which keeps
its existing internal robust-minimum tolerance). Added
`tests/test_pct_change_guard.py`, a repo-wide regression guard that fails on any new
unguarded `pct_change()` call in a production price-return path. 768 signals tests,
218 backtesting tests, 30 portfolio tests, and 100 reporting/dashboard tests pass.

**Adversarial-review fix round:** the same fabrication class survives in indicators
that never call `pct_change()`. Fixed: RSI family (`rsi_14`, `rsi_14_raw`, `rsi_28`,
`stoch_rsi_14`) and `ease_of_movement_14d`/`force_index_13d`, where EWM with pandas'
default `ignore_na=False` decays through a missing session's diff/flow input and
emits a frozen duplicate value on/after a gap — now gated with `require_full_window`
over the estimator's nominal span; `ad_line_momentum_21d` and `chaikin_oscillator`
(ungated `cumsum` flow, identical to the OBV/PVT defect) — now gated on the trailing
flow window; `money_flow_index_14d` (`.where(tp_change > 0, 0.0)` fabricates a zero
flow on gap days) — gated on trailing `tp_change` validity; `chaikin_money_flow_21d`
`min_periods` raised to full window; `ppo_12_26` masked so it cannot emit a
duplicate value on a session with no price bar. Guard scan broadened to
`execution/`, `risk/`, `airflow/`, `scripts/`, `data/`. See the "non-pct_change
fabrication sweep" section of `docs/plans/01b1-pct-change-inventory.md`.

## P1 / High findings

### BUG-011: Approval gate trusts any DB row with matching run ID and SHA

**Severity:** High / authorization

**Evidence:** The Airflow approval sensor accepts a row in `blotter_approvals` matching run ID and expected SHA, then pushes selected IDs and quantity overrides. There is no authenticated operator boundary, signer verification, role check, or DB least-privilege separation. The approval table accepts arbitrary JSON selected IDs/overrides.

**Impact:** Any actor with DB write access can approve, deselect, or alter orders.

**Suggested direction:** Move approval through an authenticated service/API, store immutable principal/session evidence, validate selected IDs/overrides against the artifact, and restrict DB write privileges.

### BUG-012: Circuit breaker is UI-local/in-memory and not enforced at Airflow submission

**Severity:** High / trading safety

**Evidence:** The dashboard circuit breaker is a Streamlit cached singleton. The paper risk/compliance script explicitly forces `circuit_breaker_open=False`, and the Airflow submission path does not query a durable circuit-breaker state before submitting.

**Impact:** Airflow can submit orders while the dashboard shows an open breaker, or after a breach that is not represented in the DAG process.

**Suggested direction:** Persist circuit-breaker state in Postgres/Redis with audit records and require submission tasks to fail closed if it is open or unavailable.

### BUG-013: Service exposure and weak/no auth defaults create high-impact compromise paths

**Severity:** High / security

**Evidence:** Compose publishes Postgres, Redis, MinIO, MLflow, Airflow, Prometheus, Loki, and Grafana ports. Redis lacks auth/TLS, MLflow binds to `0.0.0.0`, and broad DB access can affect approval/control-plane state.

**Impact:** Running the stack on a shared workstation, remote VM, Codespace, or exposed network can expose secrets, data, and order-approval controls.

**Suggested direction:** Bind local-only services to `127.0.0.1`, avoid publishing internal services, require auth, and split DB users by privilege.

### BUG-014: Dashboard approval identity is spoofable or can be `unknown`

**Severity:** High / auditability

**Evidence:** Dashboard session state derives `operator_email` from `OPERATOR_EMAIL`, defaulting to `unknown`, and writes it directly into approval records.

**Impact:** The audit trail can contain approvals by `unknown` or any environment-provided string, weakening non-repudiation.

**Suggested direction:** Require authenticated dashboard users and store immutable identity metadata and signed approval payloads.

### BUG-015: Blotter approval UI can present/approve the wrong pending run

**Severity:** P1 / approval workflow

**Evidence:** The dashboard recursively scans `**/blotter*.json` under the artifact directory and returns the newest unapproved run. It is not bound to the currently waiting Airflow DAG run or XCom-published path/hash.

**Impact:** A stale, copied, test, or attacker-placed blotter can be presented first; the operator may approve the wrong artifact while Airflow keeps waiting or fails on mismatch.

**Suggested direction:** Bind the UI to Airflow metadata for the current waiting run, enforce canonical paths, and validate the exact expected hash.

### BUG-016: Blotter approval page does not validate full blotter schema before approval

**Severity:** P1 / safety gate validation

**Evidence:** The CLI submit validator requires exact schema/safety fields, but the dashboard only checks `candidate_rows_sha256` if present and then renders `candidate_rows`.

**Impact:** Operators can approve malformed artifacts that the downstream submit path rejects or that do not represent valid stage-only blotters.

**Suggested direction:** Share schema validation code between CLI/submission and dashboard, and block display/approval on validation errors.

### BUG-017: Quantity-reduction workflow updates `quantity` but validation still checks `estimated_shares`

**Severity:** P1 / workflow mismatch

**Evidence:** The UI stores reduced quantities in `quantity_overrides`; Airflow applies them to `quantity`; the validator checks `estimated_shares` instead.

**Impact:** Operator-approved quantity reductions may still fail validation on the original field, and validation does not necessarily cover the quantity that will be submitted.

**Suggested direction:** Make a single canonical submitted-quantity field and validate exactly that field after all overrides are applied.

### BUG-018: Project Python version metadata conflicts with Airflow image

**Severity:** P1 / runtime consistency

**Evidence:** `pyproject.toml` requires Python `>=3.12`, while the Airflow Docker image uses Python 3.11.

**Impact:** Installing the project into the Airflow image may be rejected, and development/test and orchestration runtimes are split.

**Suggested direction:** Align supported Python versions or use a Python 3.12 Airflow/runtime image if possible.

### BUG-019: MLflow artifact bucket config is ignored by MLflow server command

**Severity:** P1 / config drift

**Evidence:** `minio-init` creates `MINIO_BUCKET_MLFLOW`, but the MLflow server command hard-codes `s3://mlflow`.

**Impact:** Changing the bucket env var creates one bucket while MLflow writes to another.

**Suggested direction:** Use the same env var in the MLflow server command.

### BUG-020: Raw snapshot path logging ignores custom raw bucket names

**Severity:** P1 / auditability

**Evidence:** `save_raw_response()` writes to `MINIO_BUCKET_RAW` but returns/logs `rqis-raw/{key}` regardless of the configured bucket.

**Impact:** Audit/reprocessing metadata points to the wrong object location when the bucket is customized.

**Suggested direction:** Return the actual bucket/key used.

### BUG-021: Alembic migrations assume extensions exist outside migration control

**Severity:** P1 / database portability

**Evidence:** Compose init scripts create TimescaleDB/pgcrypto extensions, while migrations use `create_hypertable()` and `gen_random_uuid()` without ensuring extensions exist.

**Impact:** `alembic upgrade head` can fail on fresh non-Compose DBs, restored DBs, or old volumes missing extensions.

**Suggested direction:** Put extension creation/checks in migrations or an explicit required bootstrap step that fails clearly.

### BUG-022: Database init scripts are one-shot but required for secondary DBs

**Severity:** P1 / deployment footgun

**Evidence:** Compose relies on `/docker-entrypoint-initdb.d` to create `airflow` and `mlflow` databases; Postgres only runs those scripts on first volume initialization.

**Impact:** Existing volumes may lack required databases after scripts are added/changed, breaking Airflow/MLflow startup.

**Suggested direction:** Add an idempotent bootstrap command or documented migration/volume-reset procedure.

### BUG-023: PEG inverse rewards double-negative EPS/growth cases

**Severity:** P1 / factor definition

**Evidence:** The PEG inverse docstring says negative earnings/growth should rank poorly, but implementation multiplies earnings yield by growth. Negative EPS times negative growth becomes positive.

**Impact:** Loss-making companies with shrinking earnings can receive favorable scores.

**Suggested direction:** Require positive EPS and positive growth, or explicitly penalize negative components before scoring.

### BUG-024: Fundamental PIT safety is delegated to input dates without enforcement

**Severity:** P1 / lookahead risk

**Evidence:** Fundamental utilities state that `date` must be publication date, then forward-fill values to daily dates. There is no guardrail ensuring dates are actually public availability dates rather than fiscal period ends.

**Impact:** If period-end dates are supplied, value/quality/growth factors see fundamentals before public release.

**Suggested direction:** Require/validate an `available_at` field or enforce conservative reporting lags when true publication timestamps are absent.

### BUG-025: Missing-data weight renormalization creates coverage-driven alpha

**Severity:** P1 / ranking comparability

**Evidence:** Composite blending and top-level scoring redistribute missing signal/factor weights to available factors per row.

**Impact:** A stock with one available signal can receive a full-strength composite score, while another with complete coverage receives a diversified score. Non-random missingness can leak coverage/universe effects into alpha.

**Suggested direction:** Track coverage counts/effective weights, require minimum coverage, and optionally penalize or neutralize missingness.

### BUG-026: Indicator z-scoring drops all-tie or one-name dates

**Severity:** P1 / sample integrity

**Evidence:** Shared indicator z-scoring divides by cross-sectional standard deviation without handling zero/NaN std, while later composite blending has special handling.

**Impact:** Tied or one-name valid cross-sections become NaN and are silently dropped, altering the sample.

**Suggested direction:** Use robust z-score behavior consistently across indicator and composite layers.

## P2 / Medium findings

### BUG-027: Approval UI can crash on malformed/null quantities

**Severity:** P2 / UX robustness

**Evidence:** The approval page casts artifact/editor quantities with `int(...)` in multiple places without guarding against null, NaN, or non-numeric values.

**Impact:** Bad artifacts or invalid edits can crash the page instead of showing a clear validation error.

**Suggested direction:** Reuse downstream numeric validation before rendering and after editing.

### BUG-028: Dashboard artifact scanning lacks strong containment checks

**Severity:** P2 / confused-deputy risk

**Evidence:** `pending_blotter()` recursively opens recent `blotter*.json` files under an environment-controlled directory.

**Impact:** A process with write access to the artifact tree can influence what the dashboard presents for approval.

**Suggested direction:** Restrict to canonical current-run paths, reject symlinks, verify owner/mode, and validate schema before display.

### BUG-029: Live-trading clearance flag names differ across dashboard and broker

**Severity:** P2 / operational safety

**Evidence:** Dashboard live enablement uses `C8_CLEARED=true`; `IBKRBroker` requires `PAPER_RUN_CLEARED=true`.

**Impact:** UI and broker can disagree about whether live trading is cleared, creating confusing or unsafe runbooks.

**Suggested direction:** Use one durable, audited clearance state rather than multiple env vars.

### BUG-030: Airflow retries are risky for broker submission/control-plane actions

**Severity:** P2 / idempotency

**Evidence:** The DAG uses global retries and the submission task can retry once. Existing retry idempotency depends on a local reconciliation artifact that currently fails open when corrupt.

**Impact:** Partial failures can re-enter submission logic and duplicate orders.

**Suggested direction:** Make broker submission non-retrying unless broker-backed idempotency is implemented and fail-closed reconciliation is in place.

### BUG-031: Fundamental growth definitions use daily-row shifts after forward-fill

**Severity:** P2 / factor definition

**Evidence:** Multi-year and YoY growth indicators align fundamentals to daily dates and then shift by trading-day counts.

**Impact:** Report-event concepts can compare stale forward-filled values rather than exact quarterly/yearly report lags.

**Suggested direction:** Compute growth on report-event/fiscal-period series, then align the finished PIT metric to daily prices.

### BUG-032: `pivot_table()` silently averages duplicate ticker/date records

**Severity:** P2 / data quality

**Evidence:** Shared price and fundamentals helpers use `pivot_table`, which aggregates duplicates by mean by default.

**Impact:** Duplicate vendor rows, restatements, or split-adjustment duplicates can become synthetic averaged values without failing.

**Suggested direction:** Validate uniqueness before pivoting or use `pivot()` to fail loudly.

### BUG-033: Prometheus scrape target is not portable or backed by a Compose app service

**Severity:** P2 / monitoring correctness

**Evidence:** Prometheus scrapes `host.docker.internal:8000`, but Compose defines no app service on port 8000 and no Linux `extra_hosts` mapping.

**Impact:** Monitoring can show a dead scrape target in local Linux environments.

**Suggested direction:** Add the service/mapping or remove/update the scrape target.

## P3 / Low findings

### BUG-034: Performance table formats decimal returns as percentages incorrectly

**Severity:** P3 / UI correctness

**Evidence:** The Performance page computes returns as decimal fractions, then displays them with a percent suffix without multiplying by 100.

**Impact:** Returns/drawdowns can be understated by 100x in the UI.

**Suggested direction:** Multiply decimal fractions by 100 before percent-suffix formatting, or use Streamlit percentage formatting that expects fractions.

### BUG-035: No FastAPI/API route layer exists despite API/service-boundary expectations

**Severity:** P3 / architecture clarity

**Evidence:** Repository-wide searches found no FastAPI/APIRouter/route layer; Streamlit pages call DB query helpers directly.

**Impact:** Any expected backend validation/service boundary is absent; safety validation must be duplicated in UI and DAG code unless an API layer is added.

**Suggested direction:** Either document Streamlit-direct architecture and centralize validation libraries, or introduce an authenticated backend API boundary.

## Second-pass additions

The following findings were added after a second adversarial pass focused on gaps in data/storage/backtesting, portfolio/risk/execution, and packaging/CI/docs.

## P0 / Critical second-pass findings

### BUG-036: Package builds are blocked by an invalid PEP 517 backend

**Severity:** P0 / packaging blocker

**Evidence:** `pyproject.toml` declares `build-backend = "setuptools.backends.legacy:build"`, but the current environment cannot import that backend; `pip wheel . --no-deps --no-build-isolation` fails with `Cannot import 'setuptools.backends.legacy'`. The same metadata references `README.md`, which is not present at the repository root.

**Impact:** CI, Docker builds, deployments, and downstream consumers that build a wheel can fail before the project is installable.

**Suggested direction:** Use a valid backend such as `setuptools.build_meta` or `setuptools.build_meta:__legacy__`, restore/update the referenced README, and add a wheel-build CI check.

## P1 / High second-pass findings

### BUG-037: Same-date corporate actions overwrite each other

**Severity:** P1 / data correctness

**Evidence:** Corporate-action adjustment factors are stored in a dict keyed only by `ex_date`; split and dividend actions on the same date assign to the same key. The schema permits multiple actions for the same ticker/date when `action_type` differs.

**Impact:** A same-day split plus dividend can drop one adjustment, materially corrupting adjusted historical prices, backtests, and signal returns.

**Suggested direction:** Accumulate all action multipliers per ex-date and multiply them together, or key by `(ex_date, action_type)` before aggregating.

### BUG-038: Snapshot versions are mutable because date-only object keys are overwritten

**Severity:** P1 / reproducibility

**Evidence:** Snapshot writes use keys based on `{data_type}/{snapshot_date}/data.parquet`; pinning scripts and manifests use the caller-provided snapshot date as the version. Re-running the same snapshot date overwrites the same object path.

**Impact:** Prior backtests/manifests that point to a snapshot date can later resolve to different bytes, breaking reproducibility.

**Suggested direction:** Make snapshot paths content-addressed or run-id-addressed, refuse overwrite by default, and store/verify content hashes in manifests.

### BUG-039: Object-store errors become `FileNotFoundError`, causing corporate-action fail-open behavior

**Severity:** P1 / backtest correctness

**Evidence:** Snapshot loading converts any `S3Error` to `FileNotFoundError`; the backtest loader treats missing `corporate_actions` as optional and substitutes an empty DataFrame.

**Impact:** Auth, timeout, bucket-policy, or transient object-store failures can silently produce unadjusted backtest prices.

**Suggested direction:** Convert only true no-such-key/object-not-found errors to `FileNotFoundError`; re-raise other object-store failures and consider requiring corporate-action snapshots for production backtests.

### BUG-040: Wash-sale guard checks the wrong order direction

**Severity:** P1 / compliance correctness

**Evidence:** The trade journal finds recent loss-realizing SELL fills, but compliance stores them under a misleading `recent_loss_buys` key and `_check_wash_sale()` immediately allows every non-SELL order, blocking only later SELLs.

**Impact:** Replacement BUYs after a loss sale can pass, while unrelated later SELLs can be blocked. The control misses the likely wash-sale exposure and creates false positives.

**Suggested direction:** Rename the context to loss-sale history and evaluate replacement BUYs inside the wash-sale window, with tests for both false-negative and false-positive cases.

### BUG-041: Sector concentration is computed but never breach-checked

**Severity:** P1 / risk monitoring

**Evidence:** `RiskSnapshot` exposes `max_sector_concentration`, and the monitor computes sector weights, but breach detection checks only drawdown, VaR, beta, and single-name concentration.

**Impact:** A portfolio can be highly concentrated in one sector without tripping any sector risk breach or circuit-breaker path.

**Suggested direction:** Add configurable sector concentration thresholds and include sector breaches in the circuit-breaker decision.

### BUG-042: IBKR order-ID timeout can leave a live broker order untracked locally

**Severity:** P1 / trading safety

**Evidence:** `IBKRBroker.submit_order()` calls `placeOrder()` before waiting for a nonzero order ID. If the ID is not assigned within the timeout, it raises while warning that the order may still be live. Callers can then mark the order rejected/failed or omit the broker ID from artifacts.

**Impact:** The broker can have a working order while local OMS/artifacts show failure, encouraging duplicate retry/manual action and leaving exposure unmanaged.

**Suggested direction:** Treat post-placement ID timeouts as an indeterminate state requiring broker reconciliation; capture any available local trade/client metadata and fail closed before retrying.

### BUG-043: Non-isolated test collection fails through MLflow/pkg_resources dependency drift

**Severity:** P1 / CI reliability

**Evidence:** `backtesting.experiment_tracking.mlflow_logger` imports `mlflow` at module import time. The requirements pin `mlflow==2.10.2` but do not constrain or include a compatible `pkg_resources` provider; `python -m pytest --collect-only -q` fails with `ModuleNotFoundError: No module named 'pkg_resources'` in the current environment.

**Impact:** CI/test collection can fail before tests run, depending on setuptools/pkg_resources availability.

**Suggested direction:** Pin/add a compatible setuptools/pkg_resources provider, upgrade MLflow, or lazily import MLflow inside logger methods to avoid unrelated collection failures.

### BUG-044: Package discovery excludes operational modules used by DAGs/tests/runbooks

**Severity:** P1 / packaging completeness

**Evidence:** Setuptools package discovery includes first-party domain packages but excludes `airflow*`, `scripts*`, and `config*`. DAGs import `scripts.*` and `config.universe_loader`, and tests import `scripts` directly.

**Impact:** A wheel install can omit operational modules that the source-tree runbooks and DAGs require, making installed deployments differ from source-checkout behavior.

**Suggested direction:** Decide whether the project is installable or source-tree-only. If installable, package operational modules or move them under a first-party namespace with console entry points.

### BUG-045: Local Airflow stubs shadow real Apache Airflow imports

**Severity:** P1 / test validity

**Evidence:** The repo contains a top-level `airflow` package described as a minimal local testing stub, including simplified `DAG` and `PythonOperator` implementations under the same import path as Apache Airflow.

**Impact:** Commands run from the repo root can test against stubs rather than real Airflow, masking real DAG parse/runtime incompatibilities.

**Suggested direction:** Move stubs under a test-only namespace or inject them via fixtures, and add a real-Airflow DAG import smoke test in an environment where the stubs are absent from `PYTHONPATH`.

## P2 / Medium second-pass findings

### BUG-046: Market-data backfill can mark partially loaded tickers complete

**Severity:** P2 / ingestion completeness

**Evidence:** The resume helper treats a ticker as done if it has at least one row in the first 31 calendar days of the requested window. The yfinance backfill then skips those tickers entirely.

**Impact:** An interrupted run that wrote a few early rows can permanently skip the remaining requested history, leaving sparse coverage that downstream snapshots/signals inherit.

**Suggested direction:** Validate coverage across the full requested range using latest date, expected row-count thresholds, and/or an exchange calendar.

### BUG-047: Data-quality flag deduplication has no conflict key

**Severity:** P2 / data-quality table correctness

**Evidence:** The writer uses `ON CONFLICT DO NOTHING` for quality flags, but the migration creates only a surrogate primary key and non-unique indexes; there is no unique constraint over the logical duplicate fields.

**Impact:** Repeated data-quality checks can insert duplicate flags indefinitely, bloating dashboards and overstating unresolved issues.

**Suggested direction:** Add an appropriate unique constraint/index, such as `(ticker, date, flag_type, message)` or `(ticker, date, flag_type)`, and report actual inserted rows.

### BUG-048: `trade_fills` dedup guard allows duplicate cumulative fills with different timestamps

**Severity:** P2 / trade journal correctness

**Evidence:** The migration comment says the unique constraint rejects re-recording the same order/cumulative quantity, but the actual unique constraint includes `fill_timestamp`.

**Impact:** The same cumulative fill can be inserted again with a slightly different timestamp, corrupting FIFO P&L, wash-sale history, and position reconstruction.

**Suggested direction:** Enforce broker execution IDs or `(order_id, cumulative_filled_quantity)` idempotency in a table/constraint that can represent the true invariant.

### BUG-049: Optimizer fallbacks can return portfolios that violate configured caps

**Severity:** P2 / portfolio construction

**Evidence:** MVO infeasible paths return equal weights without re-checking max-position or sector caps. Risk parity accepts constraints but does not enforce sector caps in the solver and only applies a post-hoc single-name cap.

**Impact:** A target portfolio can be labeled optimized while violating configured position/sector constraints.

**Suggested direction:** Re-validate every fallback output against all constraints and fail rather than returning an invalid portfolio when no feasible solution exists.

### BUG-050: NaN-heavy return series can suppress VaR/CVaR breaches

**Severity:** P2 / risk monitoring

**Evidence:** VaR/CVaR helpers check raw series length before dropping NaNs, and breach checks compare values directly. NaN values do not satisfy threshold comparisons.

**Impact:** Sparse or broken return streams can produce NaN risk metrics and fail open instead of triggering a data-quality/circuit-breaker breach.

**Suggested direction:** Require a minimum number of finite observations after `dropna()`, reject non-finite VaR/CVaR, and fail closed on insufficient risk data.

### BUG-051: Step 7 CLI can submit old but checksum-valid blotters

**Severity:** P2 / trading freshness

**Evidence:** Step 6 records generation/target/snapshot dates, but Step 7 validates schema and checksums without enforcing artifact age, target date, snapshot freshness, or re-running current account/risk checks.

**Impact:** A stale blotter can be submitted against stale prices, cash, positions, and target weights as long as checksums match.

**Suggested direction:** Enforce maximum artifact age and trading-date freshness at Step 7, and require current broker/account/risk checks immediately before submission.

### BUG-052: Airflow fire-drill runbook contradicts DAG timezone semantics

**Severity:** P2 / operational docs

**Evidence:** The data DAG is documented/configured for `20:00 America/New_York`, but the fire-drill runbook says the cron fires at `20:00 UTC`.  A secondary instance of the same confusion existed as the inline `# 21:30 UTC weekdays` comment on the `schedule_interval` line of `daily_signal_pipeline.py`.

**Impact:** Operators can expect or diagnose runs at the wrong wall-clock time, especially around DST.

**Fix (Session 56 + Session 57):** Runbook scheduling notes rewritten to say ET; UTC equivalents provided for EDT and EST separately.  Inline DAG comment corrected to `# 21:30 ET weekdays (01:30 UTC in EDT / 02:30 UTC in EST)`.

### BUG-053: `make check` mutates the working tree

**Severity:** P2 / CI hygiene

**Evidence:** The `check` target depends on `fmt`, and `fmt` runs `ruff format .`, which can rewrite files before lint/typecheck/tests run.

**Impact:** A validation command can silently change files and pass locally, leaving dirty-tree formatting changes unnoticed.

**Suggested direction:** Add a non-mutating `fmt-check` target using `ruff format --check .` and make `check` depend on that instead of `fmt`.

## P3 / Low second-pass findings

### BUG-054: Fundamentals backfill skip logic can leave partially ingested tickers stale forever

**Severity:** P3 / ingestion completeness

**Evidence:** The fundamentals backfill considers a ticker already ingested if it has any row in `financial_statements`, and skips it unless `--force` is used.

**Impact:** Interrupted or partial fundamentals ingestion can leave concept/period coverage incomplete while the script reports the ticker as already ingested.

**Suggested direction:** Track completeness by ticker, source version, concept set, and latest filing date; skip only when the expected coverage contract is satisfied.

### BUG-064: `_write_simulation` skips shadow strategies because XCom alpha_df only covers the current run's strategy

**Severity:** P2 / multi-strategy simulation correctness

**Evidence:** `alpha_df` in `_write_simulation` comes from `ti.xcom_pull(key="alpha_scores_json", task_ids="combine_scores")`, which only contains scores for the single `params['strategy_id']` of the current DAG run. The loop `alpha_df[alpha_df["strategy_id"] == strategy_id]` returns an empty frame for every other registered strategy and `continue`s, leaving the `strategy_simulations` table unpopulated for shadow strategies.

**Impact:** The multi-strategy comparison panel in the Performance dashboard (which reads `strategy_simulations`) never receives rows for any strategy except the one that ran today, defeating the purpose of the table.

**Fix (Session 57, PR #31 Codex comment #1):** The loop now checks whether the current `strategy_id` appears in the XCom data. If yes, it uses the in-memory frame (fast path). If no, it queries `alpha_scores` from the DB for that `strategy_id` and `score_date`. All registered strategies receive a simulation row on every pipeline run.

### BUG-065: `simulated_return` denominator uses n_long instead of len(tickers), understating returns when universe < n_long

**Severity:** P2 / simulated NAV correctness

**Evidence:** `target_weights` assigns `1/len(tickers)` to each selected position (weights sum to 100% regardless of universe size). When `len(tickers) < n_long`, dividing `sum(returns)` by `n_long=20` rather than `len(tickers)` understates the portfolio return. For example, a 10-name universe where all names return 1% records only 0.5%, corrupting the compounded NAV chain.

**Impact:** Simulated NAV for small-universe strategies is systematically biased downward; strategy comparison panels understate their performance vs. larger strategies.

**Fix (Session 57, PR #31 Codex comment #2):** Changed denominator from `n_long` to `len(tickers)`. When the universe has ≥ n_long names, `len(tickers) == n_long` and the result is identical. For smaller universes, the correct portfolio return is now computed. Tickers with missing prior-day prices continue to contribute 0% (cash-equivalent treatment).

### BUG-066: Cross-sectional scoring has no minimum-eligible-count enforcement

**Severity:** P2 / research validity

**Evidence:** The 01B design plan (`docs/plans/01b-research-validity-design.md` §3.1)
requires that cross-sectional scoring "report the resulting eligible count and fail
when the configured minimum cross-section is not met." Neither
`signals/scoring/scorer.py` nor the indicator-level
`cross_sectional_zscore`/`to_long` pipeline enforces any minimum: a date whose
cross-section has shrunk to a handful of tickers (or even one) still produces
z-scores and downstream alpha scores with no warning or failure.

**Impact:** Scores computed from a silently shrunken cross-section are statistically
meaningless but indistinguishable from healthy ones downstream. The BUG-010 fix
(01B-1) makes this more visible: raising rolling `min_periods` to full windows and
gating gapped windows correctly suppresses more per-ticker values, which *increases*
the frequency of shrunken cross-sections — correct per-ticker behavior, but the
missing aggregate gate means the shrinkage stays silent.

**Suggested direction:** Belongs to the 01B research-validity follow-up work (with
BUG-008/BUG-009): add a configurable minimum eligible count to the scorer, log the
per-date eligible count, and fail closed (or mark the date ineligible) when the
cross-section is below the configured minimum rather than imputing or silently
scoring a tiny universe.

**Origin:** Confirmed during the 01B-1 (BUG-010) adversarial review, 2026-07-16;
out of scope for that fix round.

### BUG-067: Universe loader returned an empty universe on fetch failure (fail-open)

**Severity:** P1 / pipeline integrity

**Status:** Fixed on `dev/R2-01B2-pit-universe` (01B-2 Phase 3).

**Evidence:** `config/universe_loader.py::_fetch_sp500_from_wikipedia` caught all
exceptions and returned `[]`, so a Wikipedia outage silently emptied the daily
ingestion universe and every downstream consumer.

**Impact:** A transient network failure could produce a zero-ticker ingestion run
with no task failure or alert; downstream scoring would quietly shrink.

**Fix:** The loader now raises `UniverseFetchError` (fail closed); Airflow retries
absorb transient outages. The affected test was deliberately updated. The module is
also now labeled operational-current-mode only (BUG-008 type-level separation).

### BUG-068: Wikipedia constituent history has bounded count drift

**Severity:** P2 / research data quality

**Status:** Open — documented limitation of the 01B-2 initial provider; remediate
at Gate 03 with a commercial constituent source (entity-level identifiers).

**Evidence:** 2026-07-17 verification import: per-date member counts are 417
(2000-01-03), 502 (2010-01-04), 522 (2020-01-02), 519 (2023-06-01) versus a true
~503-505. 245 of 890 intervals are left-censored (assumed start at 1976-07-01);
three ticker-collision symbols (AN, SUN, AGN) are excluded entirely. See
`docs/plans/01b2-constituent-source-contract.md`.

**Impact:** Mild universe over-inclusion in the recent era and under-coverage
pre-2000. The drift adds/retains names rather than excluding removed losers, so it
does not reintroduce the BUG-008 survivorship direction, but IC cross-sections can
include a small number of non-members in older periods.

**Suggested direction:** Replace/augment the Wikipedia import with a licensed
point-in-time constituent feed at Gate 03; the schema and runtime API are already
provider-agnostic (`source`/`source_version` columns, `ConstituentProvider`
protocol).

**Update (01B-2 fix round):** ticker-collision exclusions are now persisted as a
DB-queryable JSON audit record on `universe_import_batches.excluded_tickers` and
surfaced by `coverage_report()`, so the AN/SUN/AGN exclusions no longer live only
in logs and this document's companion contract doc.

### BUG-069: Signal DAG degrades to unfiltered provisional scores when the PIT universe is stale

**Severity:** P2 / operational monitoring

**Status:** Deferred. Operator decision 2026-07-18: the warn-and-degrade
behavior is acceptable for now. Revisit flipping to hard-fail once the
universe import (`scripts/import_universe_membership.py`) is on a scheduled
cadence rather than run ad hoc — see `Worklog.md` 2026-07-18.

**Evidence:** `airflow/dags/daily_signal_pipeline.py::_pit_membership_filter`
deliberately logs a warning and proceeds without membership filtering when no
published universe import covers the score date (the import advances coverage only
when `scripts/import_universe_membership.py` is re-run).

**Impact:** Daily operational scores remain available (paper pipeline is not
blocked), but they silently revert to provisional current-membership semantics for
research purposes; the only signal is a structlog warning.

**Suggested direction:** Add an AlertManager hook or DAG-level SLA/telemetry when
the filter degrades, and consider an Airflow maintenance task that re-runs the
universe import on a schedule.

### BUG-070: Backtester uses a single full-history adjusted price series for both scoring and execution

**Severity:** P1 / backtest validity (discovered during 01B-3, BUG-009 follow-on)

**Status:** Open. Discovered while implementing 01B-3
(`docs/plans/01b-research-validity-design.md` §2.3-2.4) and deliberately left
unfixed here: the task scope for 01B-3 explicitly excludes touching execution
code, and §2.4's own required-changes list treats this as a distinct,
separately-scoped item ("Make the backtester use the raw execution series
plus explicit corporate-action accounting, then compare its total-return
valuation to the analytic series").

**Evidence:** `backtesting/loader.py::load_from_snapshot` calls the
full-history `compute_adjustment_factors`/`apply_adjustment_factors` routine
once over the entire price history (own docstring: "This loader applies
corporate-action adjustment factors before constructing DataHandler so the
backtest engine always operates on split- and dividend-adjusted closes") and
passes the single resulting adjusted series into `DataHandler`, which serves
it to both signal computation and simulated fills. 01B-3 added two
cutoff-aware alternatives
(`data/normalization/corporate_actions.build_score_price_history_as_of` /
`build_realized_total_return_as_of`) but the backtester does not use them.

**Impact:** A future corporate action within the loaded snapshot window can
still adjust a historical score/signal's input price in the backtester
specifically (the same class of lookahead BUG-009 fixed for
`signals/research/ic.py`), and the backtester's fills are computed from an
adjusted (not raw) price series rather than the raw tradable price the
design plan requires for order/cash notional (§2.2). This does not affect
IBKR paper/live order pricing (execution/ uses IBKR-quoted prices directly,
not this loader).

**Correction (adversarial review, same day):** an earlier draft of this
entry additionally claimed "this does not affect `signals/research/ic.py`
(fixed by 01B-3)". That was **false** as applied to the live IC-validation
entrypoint: `scripts/validate_signal_ic.py` and
`scripts/backfill_momentum_scores.py` loaded raw `close` directly from
`daily_prices`/the price snapshot with ZERO calls to the new cutoff-aware
builders — the timing bug (BUG-009's namesake same-close lookahead) was
fixed in `signals/research/ic.py` itself, but the adjustment gap this
BUG-070 entry describes was equally present in the live callers, not just
the backtester. **Fixed same-day**: both scripts now call
`build_score_price_history_as_of` (price-ratio-based factors: momentum,
lowvol) and `build_realized_total_return_as_of` (the forward/realized-return
leg for every factor) before prices reach `compute_ic_series`/
`compute_momentum_scores`. See BUG-071 for the residual limitation of that
fix (a single run-boundary cutoff, not a literal per-score-date cutoff).
`backtesting/loader.py` remains the one open instance of this class of gap
after that fix, which is why this entry (BUG-070) stays open.

**Suggested direction:** Split `backtesting/loader.py` into two series per
the design plan: a raw execution series (for fills/cash/share accounting,
with explicit split-share-adjustment and dividend-cash-accounting in the
portfolio path) and a cutoff-aware analytic series
(`build_score_price_history_as_of` for anything feeding signal computation,
`build_realized_total_return_as_of` for total-return valuation/reporting).
Reject a requested backtest run when the required corporate-action data
cannot be constructed for either series, rather than silently falling back
to `adj_factor=1.0`.

### BUG-071: Score-series cutoff-aware adjustment uses one run-boundary cutoff, not a literal per-score-date cutoff

**Severity:** P2 / research validity (narrow residual gap, discovered during
01B-3's P0 fix round; re-verified and re-scoped in round 4 after a
related, more severe finding on the REALIZED-RETURN leg)

**Status:** Open for the SCORE leg only, re-verified and re-scoped —
CLOSED (fixed) for the realized-return leg, which this bug originally
also covered. Do not conflate the two: they turned out to need different
verdicts.

**Round-4 history (read this first):** adversarial review round 4 found
that the single-boundary-cutoff shortcut this bug originally described for
BOTH the score series and the realized-return series was actually UNSAFE
for realized returns — an action not yet knowable at an EARLIER exit
date's own cutoff, but knowable by the later shared boundary, was
incorrectly included in that earlier exit's return: future information
leaking into a persisted, "PIT-safe" result. That was a real defect, not a
narrow edge case, and is now FIXED: `scripts/validate_signal_ic.py` uses
`signals.research.ic.compute_realized_forward_returns_as_of`, which builds
a genuinely per-exit-date-cutoff-correct series (one
`build_realized_total_return_as_of` call per distinct exit date, not one
shared boundary) — see the fixing commit for the full derivation and a
reproduction test (`TestComputeRealizedForwardReturnsAsOf` /
`TestRealizedForwardReturnsWiring`).

Per the same round's instruction, the SCORE series' original cancellation
argument was re-examined rather than assumed still valid: `scripts/
validate_signal_ic.py::_build_score_adjusted_prices` (renamed from
`_build_adjusted_price_series`, which no longer exists — it built both
series with the same flawed approach) still uses one boundary cutoff, and
this remains provably safe for a DIFFERENT structural reason than the
realized-return case: a score's ratio is always computed between two dates
INSIDE the same lookback window, both on the same side of any action whose
`ex_date` is after the window's end (the score_date) — there is no second,
later "exit" endpoint the way a realized return has, so the failure mode
round 4 found cannot arise here. See `_build_score_adjusted_prices`'s
docstring for the full re-verified derivation.

**Residual gap for the score leg:** the one case a literal per-score-date
cutoff would treat differently is an action whose `ex_date` falls exactly
ON a given score_date — not yet knowable at that exact session's own close
under the conservative rule, but includable once the global run boundary
has passed it. This is a single-session edge case per affected
ticker/action, not the "zero adjustment happens at all" gap the original
P0 fix closed, and not the "future information leaks across an entire
holdout" class of bug round 4 found (and fixed) in the realized-return
path.

**Suggested direction (score leg only):** if full per-score-date
correctness is later required for scores too, either (a) loop
score-date-by-score-date building a distinct adjusted series per date
(correctness at the cost of losing the vectorized pass's performance —
`signals.composites.*` compute an entire panel in one call, so this would
require restructuring those composites, not just this script), or (b)
special-case actions whose `ex_date` falls inside the union of all
requested score dates and mask just those specific (ticker, score_date)
pairs after the vectorized computation.

### BUG-072: Dashboard/diagnostic readers of alpha_scores/factor_scores are not filtered to the active research run

**Severity:** P2 / display correctness (discovered during 01B-3's
adversarial-review round 3; every reader except `scripts/
indicator_diagnostic.py` closed by round 7)

**Status:** Fixed. Round 8 closed the last open item
(`scripts/indicator_diagnostic.py`) — see below. Round 3 of adversarial
review on PR #35 found
that `scripts/paper_inputs_check.py` (and, through it,
`scripts/paper_target_check.py`/`paper_order_candidates_check.py`/
`paper_risk_compliance_check.py`/`paper_stage_blotter_check.py`),
`airflow/dags/daily_signal_pipeline.py`'s two internal readers, and
`scripts/pin_snapshot.py` all read `alpha_scores`/`factor_scores` without
filtering to the explicitly active `research_run_id` — the production-
safety-critical instances were fixed in round 3 (see BUG-009's status
notes). Round 4 (Codex, independently) flagged the same gap in
`reporting/dashboards/queries.py`'s `latest_alpha_scores`,
`bottom_alpha_scores`, and `factor_scores_for_ticker` and it was fixed the
same round: each now joins against an `_ACTIVE_RUN_SUBQUERY` filtering to
the active `daily_signal_pipeline_operational` run, degrading to an empty
result (not a crash) when none is active. New tests
(`tests/reporting/dashboards/test_sprint3_queries.py::
TestActiveResearchRunFiltering`) prove a stale/inactive run's colliding row
is excluded and that no-active-run degrades gracefully. Round 6 closed
`pipeline_health()`'s signals-recency check the same way — it previously
ran `MAX(computed_at)` over ALL of `alpha_scores`, so a fresher row left
behind by an inactive/superseded run could make the dashboard report the
pipeline healthy while the active run was actually stale or absent; new
tests (`tests/reporting/dashboards/test_pipeline_health.py`) reproduce
exactly that scenario and prove the active run's own staleness now wins.
Round 7 closed the remaining two: `reporting/dashboards/queries.py`'s
`alpha_score_at_fill_date`/`factor_scores_at_fill_date` (Sprint 4
audit-trail drill-down queries) and `reporting/dashboards/simulation.py`'s
`alpha_overlap_matrix` now filter via the same `_ACTIVE_RUN_SUBQUERY`
(`simulation.py` imports it directly from `queries.py` rather than
duplicating the SQL string, so the two modules cannot drift out of sync).
New tests in `tests/reporting/dashboards/test_sprint4.py`
(`TestAlphaScoreAtFillDate`, `TestFactorScoresAtFillDate`, and
`TestAlphaOverlapMatrix::test_inactive_run_row_excluded`) cover all three.
A round-7 repo-wide grep for every remaining `FROM alpha_scores`/
`FROM factor_scores` confirmed no other reader is unfiltered without a
documented, deliberate reason (see below, at the time — round 8 later
tightened the one remaining item).

**Round 8 (Codex, closing the entry):** Codex made a fair case that leaving
`scripts/indicator_diagnostic.py` open as a deliberate exception (round 7's
"research/diagnostic tool" rationale) was not actually justified once
migration 012 widened `factor_scores`' PK/unique constraints to include
`research_run_id` — this tool's own duplicate-row detector
(`backtesting/validation/indicator_diagnostic.py`'s
`indicator_diagnostic_duplicate_rows` check) only *warns* on duplicate
`(ticker, score_date, factor_name)` rows and then `pivot_table` silently
*averages* them, so a mixed-run blend could reach the reliability/validity
report with no indication it happened — a real correctness risk for a tool
whose entire purpose is measuring factor reliability/validity, not just a
staleness/display issue like the dashboard cases. Fixed:
`scripts/indicator_diagnostic.py::_load_factor_scores` now defaults to the
same `_ACTIVE_RUN_SUBQUERY` pattern used everywhere else under this bug
(scoped to the active `daily_signal_pipeline_operational` run). A new
`--all-runs` flag is the documented, explicit opt-in for genuine cross-run
diagnostic comparisons (the design plan's "explicit opt-in for cross-run
reads" principle) — and unlike the old default behavior, `--all-runs`
itself now fails closed (raises `ValueError`) if the resulting blend
contains duplicate `(ticker, score_date, factor_name)` rows, rather than
letting them reach the pivot. New tests in
`tests/test_indicator_diagnostic_script.py` cover: default active-run
scoping excludes a colliding inactive-run row, `--all-runs` raises on a
real collision, `--all-runs` succeeds when runs don't collide (disjoint
date ranges), and no-active-run degrades to an empty (not crashing) result.
`scripts/pin_snapshot.py` remains the one intentionally different pattern
(mandatory disambiguation via `--research-run-id` when a collision is
detected, rather than an active-run default) — that asymmetry is
deliberate: `pin_snapshot.py` pins a specific bundle an operator names
explicitly, while every other reader (including this one, now) defaults to
"whatever is currently operational."

**Resolution:** No remaining open scope. This entry is fully closed.

### BUG-073: pytest `testpaths` silently excluded ~412 tests from every "full suite" run

**Severity:** P1 / test-coverage integrity (self-discovered while verifying
round 8's fixes, 01B-3)

**Status:** Fixed.

**Evidence:** `pyproject.toml`'s `[tool.pytest.ini_options]` `testpaths`
listed `"tests"` AND `"tests/strategy_registry"` (a subdirectory already
inside `"tests"`) as two separate entries. Whenever pytest resolves this
full multi-entry `testpaths` list with no explicit command-line path
arguments — i.e. exactly how every round's "run affected suites + full
suite once" step in this review series actually invoked it — the presence
of that redundant nested entry silently dropped two entire subtrees from
collection: `tests/reporting/dashboards/` (113 tests — including
`test_pipeline_health.py`, `test_sprint3_queries.py`, `test_sprint4.py`,
`test_blotter_approval.py` — the exact active-research-run-filtering
regression tests added in rounds 4, 6, and 7 of this very review) and
`tests/infra/` (Airflow image/compose smoke tests), totaling 411 missing
tests (2096 truly-collectible minus 1685 actually collected). Removing the
explicit CLI path arguments and instead adding/removing individual
`testpaths` entries via `-o testpaths=...` overrides bisected the cause to
that single redundant nested entry — removing it alone (without changing
anything else) restored collection from 1685 to the full 2096.

**Impact:** Every "full suite: N passed, 0 failed" status reported to the
PM across rounds 1-8 of this review series was accurate for the tests it
ran, but was NOT actually the full suite — it silently omitted the
dashboard active-run-filtering regression tests and the Airflow
image/compose smoke tests for the entire review. Because this was
discovered and fixed with the true full suite immediately re-run and
passing (2096 passed, 0 failed, including every previously-hidden test), no
actual regression was found to have been hiding behind this gap in this
case — but the gap itself was real and could have hidden one.

**Fix:** `pyproject.toml`'s `testpaths` no longer lists
`"tests/strategy_registry"` separately; `"tests"` alone already covers it
recursively. Verified the corrected config collects 2096 tests (up from
1685) with `python -m pytest --collect-only -q`, and that all 2096 pass.

**Suggested direction (residual):** consider adding a CI-time guard (e.g. a
lightweight test or lint step) that asserts `pytest --collect-only` finds
every `test_*.py` file under the configured testpaths roots, to catch a
similar silent-exclusion regression before it can recur — not implemented
here to avoid scope creep on an already-large PR, but worth a follow-up.

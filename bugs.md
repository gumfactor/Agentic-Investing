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
| Status | `Open`, `In Progress`, `Fixed`, `Deferred`, `Won't Fix` | Current remediation state. New entries default to `Open`. |

### Fix implementation roadmap

#### Short-term / stop-the-line and near-term hardening

| Bug | Category | Severity | Fix priority | Status | Short rationale |
|-----|----------|----------|--------------|--------|-----------------|
| BUG-001 | Infra/Deploy | P0 | F0 | Open | Airflow paper DAG cannot pass its own env gate in Compose. |
| BUG-002 | Infra/Deploy | P0 | F0 | Open | Airflow image omits runtime dependencies used by DAGs. |
| BUG-003 | Infra/Deploy | P0 | F0 | Open | Paper artifacts are written to an unmounted container path. |
| BUG-004 | Infra/Deploy | P0 | F0 | Open | IBKR host defaults to container-local localhost. |
| BUG-005 | Trading Safety | P0 | F0 | Fixed | Approval quantity overrides can be tampered upward. |
| BUG-006 | Trading Safety | P0 | F0 | Fixed | Corrupt reconciliation artifacts can cause duplicate orders. |
| BUG-007 | Risk | P0 | F0 | Fixed | Risk dashboard can report zero/incorrect risk from schema mismatch. |
| BUG-008 | Research/Signals | P0 | F0 | Open | Current-membership universe creates survivorship leakage. |
| BUG-009 | Research/Signals | P0 | F0 | Open | Same-close signal/return timing can introduce lookahead. |
| BUG-010 | Research/Signals | P0 | F0 | Open | `pct_change()` defaults can distort many indicators. |
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
| BUG-030 | Trading Safety | P2 | F2 | Open | Airflow retries are risky for broker submission actions. |
| BUG-031 | Research/Signals | P2 | F2 | Open | Fundamental growth uses daily-row shifts after forward-fill. |
| BUG-032 | Data/Storage | P2 | F2 | Open | `pivot_table()` silently averages duplicate records. |
| BUG-033 | Infra/Deploy | P2 | F2 | Open | Prometheus scrape target is not backed by Compose service. |
| BUG-046 | Data/Storage | P2 | F2 | Open | Market-data backfill can mark partial loads complete. |
| BUG-047 | Data/Storage | P2 | F2 | Open | Data-quality flag dedupe has no conflict key. |
| BUG-048 | Trading Safety | P2 | F2 | Open | Trade-fill dedupe allows duplicate cumulative fills. |
| BUG-049 | Portfolio | P2 | F2 | Open | Optimizer fallbacks can violate configured caps. |
| BUG-050 | Risk | P2 | F2 | Open | NaN-heavy return series can suppress VaR/CVaR breaches. |
| BUG-051 | Trading Safety | P2 | F2 | Open | Step 7 CLI can submit old checksum-valid blotters. |
| BUG-052 | Docs/Process | P2 | F2 | Open | Fire-drill runbook contradicts DAG timezone semantics. |
| BUG-053 | Packaging/CI | P2 | F2 | Open | `make check` mutates the working tree. |

#### Long-term / lower-risk backlog

| Bug | Category | Severity | Fix priority | Status | Short rationale |
|-----|----------|----------|--------------|--------|-----------------|
| BUG-034 | Dashboard/API | P3 | F3 | Open | Performance table formats decimal returns as percentages incorrectly. |
| BUG-035 | Dashboard/API | P3 | F3 | Open | No FastAPI/API route layer exists despite service-boundary expectations. |
| BUG-054 | Data/Storage | P3 | F3 | Open | Fundamentals backfill skip logic can leave partial ingestions stale. |

## Startup / repository state

- Current branch observed: `work`.
- No configured `origin` remote or `origin/<branch>` tracking branch was visible from `git remote -v` / branch checks at review startup.

## P0 / Critical findings

### BUG-001: Airflow paper-trading DAG cannot pass its own environment gate under Docker Compose

**Severity:** P0 / deployment blocker

**Evidence:** `docker-compose.yml` passes database, Redis, MinIO, and Polygon settings into Airflow, but does not pass `PAPER_TRADING`, `IBKR_PORT`, `IBKR_HOST`, or `IBKR_CLIENT_ID`. The paper DAG fails fast unless `PAPER_TRADING=true` and `IBKR_PORT=7497` are present. `.env.example` defines those variables, but they are not injected into the Airflow service environment.

**Impact:** The Compose-managed `daily_paper_trading` DAG will fail before performing useful work.

**Suggested direction:** Pass the IBKR/paper env vars into all Airflow containers, and add a deployment smoke test that imports the DAG and runs `_require_paper_env()` against the container environment.

### BUG-002: Airflow image omits runtime dependencies used by DAG execution paths

**Severity:** P0 / deployment blocker

**Evidence:** `infra/docker/Dockerfile.airflow` installs only a short hand-picked package list, while DAG execution paths import `pandas`, `pyarrow`, `ib_insync`, database drivers, and other packages listed in `requirements.txt`.

**Impact:** The built Airflow runtime is likely to fail at task runtime with `ModuleNotFoundError`, especially for Parquet/MinIO and IBKR paper-trading paths.

**Suggested direction:** Install the project package and/or `requirements.txt` in the Airflow image, then test DAG imports inside the built image.

### BUG-003: Paper-trading artifacts are written to an unmounted container path

**Severity:** P0 / approval workflow blocker

**Evidence:** The paper DAG defaults artifacts to `/opt/airflow/rqis_paper` and documents it as a shared Docker Compose volume. Compose does not mount that path into Airflow containers or the dashboard.

**Impact:** Blotter artifacts can be invisible to host-side approval tools/dashboards and may disappear on container recreation.

**Suggested direction:** Add a named volume or host bind mount for `RQIS_PAPER_ARTIFACT_DIR` shared by Airflow and the dashboard/approval tooling.

### BUG-004: IBKR connectivity defaults to container-local localhost

**Severity:** P0 / broker connectivity blocker

**Evidence:** `IBKRBroker` defaults to `IBKR_HOST=127.0.0.1`; `.env.example` does the same; the DAG constructs `IBKRBroker()` without a host. In Docker, `127.0.0.1` is the container, not the host running TWS/IB Gateway.

**Impact:** Even with env vars passed, containerized paper trading will usually fail to reach the broker socket.

**Suggested direction:** Document and configure a Docker-safe host (`host.docker.internal` plus Linux `extra_hosts`, host networking, or explicit gateway IP), and require a connectivity preflight in containerized runs.

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

**Evidence:** The research universe is documented as current-membership S&P 500 and excludes removed constituents. The IC engine merges scores to forward returns on available ticker/date rows without enforcing point-in-time membership.

**Impact:** Historical IC can be biased upward by excluding bankrupt, removed, or underperforming names that were in the investable universe at the time.

**Suggested direction:** Add a PIT constituent/membership table and require IC computations to filter by membership as of each signal date.

### BUG-009: Same-close signal/return timing can introduce lookahead

**Severity:** P0 / research and backtest validity

**Evidence:** Forward returns are computed from signal-date close to future close. Many price signals use the signal-date close in the signal itself.

**Impact:** Unless the project explicitly assumes known-before-close signals and close/auction execution, IC/backtests can include a one-bar lookahead.

**Suggested direction:** Shift signals or forward-return windows to the next executable bar, or enforce timestamped market-on-close assumptions.

### BUG-010: `pct_change()` missing-data defaults distort many indicators

**Severity:** P0 / signal correctness

**Evidence:** Multiple indicators call `pct_change()` without `fill_method=None` on wide data containing NaNs.

**Impact:** Pandas can forward-fill missing prices before return calculations, creating artificial zero returns, suppressed volatility/beta, and distorted volume-price signs.

**Suggested direction:** Use `pct_change(fill_method=None)` consistently and require sufficient non-null observations per ticker/window.

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

**Evidence:** The data DAG is documented/configured for `20:00 America/New_York`, but the fire-drill runbook says the cron fires at `20:00 UTC`.

**Impact:** Operators can expect or diagnose runs at the wrong wall-clock time, especially around DST.

**Suggested direction:** Update the runbook to state actual Airflow scheduling semantics and provide UTC examples for standard/daylight time.

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

# Adversarial Review Findings

Review date: 2026-06-30

This file consolidates an adversarial, multi-theme review of the project. It is intentionally written as a handoff queue for later remediation; no fixes are included here.

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

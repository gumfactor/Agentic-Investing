# RQIS — Engineering Worklog

This file is the canonical running record of all work performed on the Robust Quant Investment System.
Every session must append a dated entry. Every significant decision, trade-off, or "why did we do it this way" must be recorded here.

**Convention:**
- Entries are newest-first within each date block.
- Decision records are prefixed `[DECISION]`.
- Risk / safety notes are prefixed `[SAFETY]`.
- Blockers are prefixed `[BLOCKER]`.
- Resolved items are prefixed `[RESOLVED]`.

---

## 2026-06-05

### Session 6 — Fix Alembic migration config bugs found during live stack validation

**Operator:** mshane@thecanadalist.ca  
**Branch:** `claude/quant-system-prd-koDnJ`  
**Commits this session:** (see git log)

---

#### What was done

Operator ran `make migrate` and hit a failure. Two bugs in the Alembic config fixed.

**Bug 1 — `alembic.ini`: ConfigParser interpolation error on `%(DATABASE_URL)s`**

Root cause: `%(KEY)s` is Python ConfigParser interpolation syntax. It looks for
`KEY` as a variable defined elsewhere in the same ini file — it does NOT read
from environment variables. The key `DATABASE_URL` was never defined in
`alembic.ini`, so ConfigParser raised `InterpolationMissingOptionError` before
`env.py` even ran.

Fix: Replaced the interpolation placeholder with a non-interpolated sentinel
string `not-set-see-env-py`. Since `env.py` calls
`config.set_main_option("sqlalchemy.url", ...)` at runtime, the ini value is
never used; it just needs to not crash ConfigParser.

**Bug 2 — `env.py`: `.env` never loaded, `DATABASE_URL` always `None`**

Root cause: `env.py` called `os.environ.get("DATABASE_URL")` but never loaded
`.env` first. Running `alembic upgrade head` from a shell that hasn't exported
`DATABASE_URL` meant the variable was always `None`. `config.set_main_option`
was silently skipped, leaving the dummy ini value in place, which then caused
a confusing SQLAlchemy connection error.

Fix:
- Added `load_dotenv()` (from `python-dotenv`, already in `requirements.txt`)
  before reading `os.environ`. `load_dotenv()` searches the cwd and all parent
  directories, so it works from any working directory.
- Changed the `if database_url:` guard to a hard `raise RuntimeError` with a
  clear message if the URL is still absent after loading `.env`. Fail loud and
  early rather than producing a confusing downstream error.

---

#### Next steps
Re-run `make migrate` — should apply migration 001 cleanly now.

---

### Session 5 — Fix docker-compose.yml bugs found during live stack validation

**Operator:** mshane@thecanadalist.ca  
**Branch:** `claude/quant-system-prd-koDnJ`  
**Commits this session:** (see git log)

---

#### What was done

Operator ran `make up` locally and hit two real bugs in `docker-compose.yml`. Both fixed.

**Bug 1 — `airflow-init` broken multiline command**

Root cause: YAML `>` folding scalar folds newlines into spaces. A multiline
`bash -c "... \n airflow users create \n --username admin \n ..."` string
looked fine in the file but the newlines inside the bash double-quoted string
produced parse errors when Docker executed the command.

Fix: Replaced the folded string form with a YAML list form. The entire
`bash -c` argument is now a single unambiguous quoted string:
```yaml
command:
  - bash
  - -c
  - "airflow db migrate && airflow users create --username admin ..."
```

**Bug 2 — MLflow missing `psycopg2` and `boto3`**

Root cause: `ghcr.io/mlflow/mlflow:v2.10.2` is a minimal image. It does not
ship `psycopg2-binary` (needed for `--backend-store-uri postgresql+psycopg2://`)
or `boto3` (needed for `--artifacts-destination s3://` against MinIO).
MLflow starts but immediately crashes when it tries to connect to either backend.

Fix: Added `infra/docker/Dockerfile.mlflow` which extends the base image and
installs both packages. Updated docker-compose.yml `mlflow` service to use
`build:` instead of `image:`.

Note: First `make up` after this fix will build the MLflow image locally
(~2 min). Subsequent starts use the cached layer.

---

#### [DECISION] Thin custom Dockerfile for MLflow rather than entrypoint hack
Rationale: An alternative fix is `entrypoint: bash -c "pip install ... && mlflow server ..."`.
That reinstalls packages on every container restart, adding 30–60 seconds to
every start. A build-time install is permanent in the image layer — same result,
no runtime cost.

---

#### Next steps
Same as Session 4 — operational validation on operator's machine.

---

### Session 4 — Phase 1 Unit Test Coverage: Fill Gaps

**Operator:** mshane@thecanadalist.ca  
**Branch:** `claude/quant-system-prd-koDnJ`  
**Commits this session:** (see git log)

---

#### What was done

Audited Phase 1 test coverage. Three source files had zero tests; the base
client had one untested method. Wrote four new test files to close all gaps.

**Gap analysis:**

| Source file | Had tests? | Action |
|---|---|---|
| `data/ingestion/market/base_client.py` | No | New: `test_base_client.py` |
| `data/ingestion/market/yfinance_client.py` | Yes (from Session 3) | — |
| `data/normalization/corporate_actions.py` | Yes | — |
| `data/normalization/point_in_time.py` | Yes | — |
| `data/normalization/quality_checks.py` | Yes | — |
| `data/storage/timescale_writer.py` | **No** | New: `test_timescale_writer.py` |
| `data/storage/parquet_snapshots.py` | **No** | New: `test_parquet_snapshots.py` |
| `config/universe_loader.py` | **No** | New: `test_universe_loader.py` |

**New test files:**
- `data/tests/test_base_client.py` — 6 tests: `validate_date_range`, frozen dataclass, optional fields
- `data/tests/test_timescale_writer.py` — 26 tests: upsert SQL contract (ON CONFLICT DO UPDATE), batching, missing-column validation, `_to_decimal_or_none`, `_to_int_or_none` edge cases
- `data/tests/test_parquet_snapshots.py` — 18 tests: save/load/list round-trip, object key format, FileNotFoundError on missing snapshots, bucket auto-creation
- `data/tests/test_universe_loader.py` — 16 tests: Wikipedia source, CSV source, force include/exclude, deduplication, error handling

**Two pre-existing test bugs fixed:**
- `test_no_false_positive_on_normal_movement` in quality checks: date arithmetic overflowed January (day > 31). Fixed with `timedelta(days=i)`.
- `test_multi_ticker_extracts_correct_rows` in yfinance client: mock MultiIndex fixture was missing `High`/`Low` columns. Fixed by constructing a complete fixture.

**Final result:** 120 tests, all passing, no live services required.

---

#### [DECISION] TimescaleWriter tests mock the SQLAlchemy engine, not the DB
Rationale: Unit tests for the writer should verify SQL generation and parameter passing, not PostgreSQL behaviour. Integration tests against a real DB belong in `tests/integration/` and require `make up` — they are marked with `@pytest.mark.integration` and excluded from the default `make test` run.

---

#### Next steps

Phase 1 exit criterion is now fully covered by tests. Remaining operational steps:
1. Operator: `make up && make migrate && make backfill` on local machine
2. Monitor quality flags from backfill run
3. Pin the dataset snapshot version in MLflow
4. Begin Phase 2: SimFin fundamental data client

---

### Session 3 — Phase 1 Build: Full Data Foundation

**Operator:** mshane@thecanadalist.ca  
**Branch:** `claude/quant-system-prd-koDnJ`  
**Commits this session:** (see git log)

---

#### What was built

**Infrastructure:**
- `docker-compose.yml` — complete 9-service local stack: TimescaleDB, Redis, MinIO, MLflow, Airflow (webserver + scheduler + init), Prometheus, Loki, Grafana
- `infra/db/init/01_create_databases.sql` — creates `airflow` and `mlflow` databases at container first boot
- `infra/db/init/02_extensions.sql` — enables TimescaleDB, pgcrypto, pg_stat_statements
- `infra/prometheus/prometheus.yml` — Prometheus scrape config
- `infra/grafana/provisioning/datasources/datasources.yaml` — auto-provisions Prometheus and Loki data sources in Grafana

**Python project setup:**
- `pyproject.toml` — package discovery, pytest config, ruff linter config, mypy strict config
- `requirements.txt` — all production dependencies pinned to specific versions
- `requirements-dev.txt` — test/lint dependencies
- `Makefile` — developer convenience targets: `make up/down/clean/migrate/backfill/test/lint/typecheck/fmt`

**Database schema:**
- `data/storage/schema/market.sql` — canonical SQL reference for all Phase 1 tables (daily_prices, corporate_actions, data_ingestion_log, data_quality_flags) with inline documentation
- `data/storage/schema/fundamentals.sql` — Phase 2 placeholder with PIT correctness design notes
- `data/storage/schema/signals.sql` — Phase 2 placeholder
- `alembic.ini` — Alembic config with DATABASE_URL read from environment (no hardcoded credentials)
- `infra/db/migrations/env.py` — Alembic env file
- `infra/db/migrations/versions/001_initial_market_schema.py` — full reversible migration for all Phase 1 tables including TimescaleDB hypertable creation

**Folder skeleton:**
- All 40+ directories from PRD Section 5 created with `__init__.py` (Python packages) or `.gitkeep` (non-Python dirs)

**Data ingestion layer:**
- `data/ingestion/market/base_client.py` — abstract `BaseMarketDataClient` with `OHLCVBar` and `CorporateActionRecord` dataclasses; Decimal pricing throughout
- `data/ingestion/market/yfinance_client.py` — full yfinance implementation: batched downloads, multi/single ticker normalisation, corporate actions fetch, CLI `backfill` entry point

**Data normalisation layer:**
- `data/normalization/quality_checks.py` — 5 check types: negative prices, HLOC violations, zero volume, price jump detection (rolling z-score), universe completeness
- `data/normalization/corporate_actions.py` — cumulative adjustment factor computation (splits backward-walking algorithm, dividend ex-date adjustment); `apply_adjustment_factors()` for OHLCV
- `data/normalization/point_in_time.py` — `pit_join()`, `pit_latest()`, `add_ohlcv_release_date()`; documented look-ahead bias prevention with release_date semantics

**Data storage layer:**
- `data/storage/timescale_writer.py` — `TimescaleWriter` with idempotent upserts for OHLCV, corporate actions, quality flags; ingestion log write/read; batched inserts; Decimal-safe writes
- `data/storage/parquet_snapshots.py` — `ParquetSnapshots` for MinIO read/write; snapshot versioning; raw API response archiving for idempotent reprocessing

**Configuration:**
- `config/settings.yaml` — all tunable parameters with documented units and defaults
- `config/universe.yaml` — universe source and eligibility filter definitions
- `config/universe_loader.py` — `load_universe()` fetching S&P 500 from Wikipedia; CSV fallback; force include/exclude overrides

**Orchestration:**
- `airflow/dags/daily_data_pipeline.py` — full Airflow DAG with 9 tasks: fetch_universe → fetch_ohlcv → quality_checks → write_flags/write_ohlcv/save_snapshot; parallel corporate actions track; XCom-based data passing; 3× retry with exponential backoff

**Tests (50+ test cases):**
- `data/tests/test_point_in_time.py` — 14 tests covering the critical look-ahead-bias gates including release_date lag on fundamentals
- `data/tests/test_quality_checks.py` — 15 tests across all 5 check types
- `data/tests/test_corporate_actions.py` — 9 tests for split/dividend factor computation and application
- `data/tests/test_yfinance_client.py` — 13 tests with mocked yfinance API

---

#### Key decisions recorded

**[DECISION] `daily_prices` stores unadjusted prices; adjusted prices computed from `corporate_actions`**  
Rationale: Storing unadjusted prices with a separate corporate actions table makes every adjustment auditable and reversible. If an adjustment is found to be wrong, we fix the corporate action record and recompute — we never lose the original prices. Source-provided adjusted closes are stored in `source_adj_close` for cross-validation only.

**[DECISION] Decimal (not float) for all prices throughout the stack**  
Rationale: Floating-point representation errors accumulate across adjustment factor multiplications. A 2-for-1 split applied to 252 daily closes produces measurable rounding differences in float vs. Decimal arithmetic. The schema uses `NUMERIC(18,6)`; Python code uses `Decimal`. This is a correctness requirement, not a style preference.

**[DECISION] Ingestion pipeline is fully idempotent (upserts, not inserts)**  
Rationale: Airflow tasks retry on failure. If a task succeeds but Airflow marks it failed due to a timeout, re-running it must produce the same result. All DB writes use `ON CONFLICT DO UPDATE`, so rerunning is always safe.

**[DECISION] Raw API responses stored in MinIO before any transformation**  
Rationale: If a transformation bug is discovered after the fact, we can re-run the transformation against the archived raw data without hitting the API again. This also satisfies the C7 data-version audit requirement — the raw_storage_path in `data_ingestion_log` gives a permanent record of the exact data received.

**[DECISION] `pit_join()` requires explicit `release_date_col` for non-OHLCV data**  
Rationale: Making the caller explicitly specify the release date column prevents accidentally using `date` as a proxy for release date (which is wrong for fundamentals). The function raises `KeyError` if the column doesn't exist, rather than silently falling back — fail loud is preferable to silent look-ahead bias.

**[DECISION] Airflow uses XCom for inter-task data passing (not shared filesystem)**  
Rationale: XCom is Airflow-native and works whether tasks run on the same or different workers. The data volumes in Phase 1 (daily S&P 500 bars ≈ 500 rows × 8 columns ≈ small JSON) are well within XCom size limits. For larger datasets in later phases, replace with MinIO path passing (fetch → write to MinIO → pass path via XCom).

**[DECISION] Wikipedia S&P 500 fetch for Phase 1 universe (survivorship bias caveat documented)**  
Rationale: No paid data source is available in Phase 1. Wikipedia gives current membership, which introduces survivorship bias (companies that were removed from the index are excluded from backtests). This is explicitly documented in `config/universe_loader.py` and `config/universe.yaml` as a Phase 1 limitation. Phase 2 replaces with Polygon constituent history.

**[SAFETY] `make clean` requires interactive `YES` confirmation**  
Rationale: `docker compose down -v` is irreversible — it destroys all local data. The Makefile target wraps this in a `read -p` confirmation gate, consistent with C9 in the PRD. This cannot be bypassed by piping input from another command in a normal shell session.

---

#### Phase 1 exit criterion progress

| Criterion | Status |
|-----------|--------|
| Infrastructure stack runnable | ✅ docker-compose.yml complete |
| Database schema deployed | ✅ Migration 001 ready (`make migrate`) |
| OHLCV ingestion working | ✅ yfinance_client.py + Airflow DAG |
| Corporate actions pipeline | ✅ fetch + normalise + write |
| Point-in-time correctness | ✅ pit_join() with tests |
| Quality checks operational | ✅ 5 check types with tests |
| 5 years of data in DB | ⏳ Run `make backfill` after `make up && make migrate` |
| Data quality green | ⏳ Requires live backfill run |

---

#### Next steps (remaining Phase 1 work)

1. Run `make up` and `make migrate` on operator's machine to provision the stack
2. Run `make backfill` to pull 5 years of S&P 500 OHLCV
3. Review quality flags produced by backfill — resolve any `severity=error` flags
4. Verify Airflow daily pipeline runs clean for one full week
5. Snapshot the backfilled data (`ParquetSnapshots.save_snapshot()`) to pin the Phase 1 dataset version
6. Begin Phase 2 planning: SimFin fundamental data client

---

### Session 2 — Operator Configuration Decisions

**Operator:** mshane@thecanadalist.ca  
**Branch:** `claude/quant-system-prd-koDnJ`  
**Commits this session:** (pending — configuration decisions logged before build begins)

---

#### What was done

Operator answered four blocking pre-build clarification questions. Answers override or refine PRD defaults for all subsequent implementation work.

---

#### Operator decisions recorded

**[DECISION] Data source: yfinance (free tier) for Phase 1**  
Operator does not have a Polygon.io subscription. Phase 1 will use `yfinance` for daily OHLCV and fundamental data. The data ingestion module will be written against an abstract `DataClient` interface so Polygon (or any other provider) can be swapped in via config in a later phase without rewriting consuming code. This means no real-time feed in Phase 1 — daily bars only, which is appropriate given we are not executing intraday.  
*Impact on PRD:* F1.1 "Polygon.io (primary)" deferred to Phase 2+. yfinance is Phase 1 primary.

**[DECISION] Deployment: Local machine via Docker Compose**  
All infrastructure (TimescaleDB, MinIO, Redis, Airflow, MLflow, Prometheus/Grafana) will run as Docker Compose services on a local machine. No cloud provisioning in Phase 1. Config will use environment variables throughout so a future cloud migration is a matter of changing env vars, not code. No Terraform or cloud-specific IaC in v1.  
*Impact on PRD:* `infra/` folder will contain `docker-compose.yml` and `Dockerfile` as the primary delivery. Kubernetes / cloud configs deferred.

**[DECISION] Broker: IBKR only (no Alpaca)**  
Operator has an IBKR account. Alpaca will not be built. The execution layer will have:
- `IBKRBroker` as the sole concrete broker implementation
- IBKR natively supports a paper trading account (separate TWS/Gateway paper session on port 7497 vs. live on port 7496) — this is how paper vs. live will be differentiated, controlled by `IBKR_PORT` env var and the `PAPER_TRADING=true/false` flag
- The `BaseBroker` abstract interface will still be written so a second broker can be added later without changing OMS code
- Alpaca references in the PRD's tech stack section are superseded by this decision  
*Impact on PRD:* F4.4 "Alpaca" broker integration dropped from v1. `execution/brokers/alpaca_broker.py` will not be created. IBKR paper port (7497) serves the paper trading phase gate (C8).

**[SAFETY] IBKR paper vs. live port separation**  
Because IBKR uses the same `ib_insync` client for both paper and live, the only difference is the port number (7497 paper / 7496 live). This is a single config value that, if changed accidentally, would route paper orders to a live account. Safeguard:  
- `PAPER_TRADING=true` env var must be set to route to port 7497  
- At startup, the broker client will log a clearly visible warning: `⚠ PAPER TRADING MODE — connected to port 7497` or `🔴 LIVE TRADING MODE — connected to port 7496`  
- Switching `PAPER_TRADING` from `true` to `false` is treated as a C9 destructive action requiring `"YES"` confirmation  
- A CI test will assert that `PAPER_TRADING=true` always maps to port 7497

**[DECISION] Team structure: Solo now, designed for handoff**  
No multi-user auth or role-based access controls in v1. However:
- Every module gets a brief module-level docstring explaining its responsibility and key invariants
- All public functions get type hints and one-line docstrings
- The Worklog stays comprehensive so a new team member can read recent entries and understand current state
- Interface boundaries between layers (data / signals / portfolio / execution / risk) are kept clean — no cross-layer imports that bypass the defined interface
- No single-operator shortcuts that would require rewriting for a team (e.g., no hardcoded paths, no personal-machine assumptions in Docker configs)

---

#### What changes from PRD defaults (summary)

| Topic | PRD default | Actual build target |
|-------|-------------|---------------------|
| Data source (Phase 1) | Polygon.io primary | yfinance primary |
| Infrastructure | Docker Compose + K8s migration path | Docker Compose only; env-var-ready for cloud |
| Broker | IBKR (live) + Alpaca (paper) | IBKR only; paper via port 7497 |
| Paper trading env | Alpaca paper API | IBKR paper account (TWS port 7497) |
| Team access controls | Single-operator for now | Clean interfaces, no multi-user auth yet |

---

#### Next steps (Phase 1 build begins next session)

1. Create full folder skeleton with `.gitkeep` files
2. Write `docker-compose.yml` for local stack (TimescaleDB, MinIO, Redis, Airflow, MLflow, Prometheus, Grafana)
3. Write `.env.example` with all required variables
4. Write `pyproject.toml` + `requirements.txt`
5. Write TimescaleDB schema SQL for `daily_prices` and `corporate_actions` tables
6. Write Alembic migration setup
7. Write `data/ingestion/market/yfinance_client.py` with quality checks
8. Write `data/normalization/point_in_time.py`

---

### Session 1 — Project Initialization

### Session 1 — Project Initialization

**Operator:** mshane@thecanadalist.ca  
**Branch:** `claude/quant-system-prd-koDnJ`  
**Commits this session:** `6640e50`

---

#### What was done

1. **Repository initialized** on branch `claude/quant-system-prd-koDnJ`.
   - The repo was completely empty at session start — no files, no prior commits.

2. **`PRD.md` authored and committed** (921 lines, commit `6640e50`).
   - Full Product Requirements Document for the Robust Quant Investment System.
   - Covers all seven system layers: Data, Signal Generation, Portfolio Construction, Execution, Risk & Monitoring, Backtesting, Reporting.
   - Covers the Claude Code skill architecture (11 skills with MCP server interfaces).

3. **`Worklog.md` created** (this file).
   - Establishes the pattern for session-by-session documentation.

4. **`CLAUDE.md` created** (project context file for Claude Code sessions).
   - Gives any future Claude Code session immediate orientation to the project.

---

#### Key decisions recorded

**[DECISION] PRD written before any code**  
Rationale: The system touches live brokerage accounts and real capital. Writing a comprehensive PRD first ensures all safety constraints, approval gates, and architectural decisions are explicit and reviewed before a single line of executable code is written. A PRD-first approach also makes every later build decision traceable to a requirement.

**[DECISION] Phase-gated milestones (5 phases over 36 weeks)**  
Rationale: The system has hard dependencies between layers — you cannot validate signals without clean data; you cannot paper-trade without a working OMS; you cannot go live without paper-trading for 4 weeks. A linear phase gate prevents the temptation to skip steps that protect capital.

**[DECISION] Nine safety/reversibility constraints codified in PRD (C1–C9)**  
Rationale: Quantitative trading systems have historically failed catastrophically not from bad signals but from operational errors — runaway orders, corrupt data silently influencing live positions, audit trails that could not reconstruct what happened. Encoding these as named, numbered constraints (not vague guidelines) means they can be referenced by constraint ID in code reviews, incident reports, and runbooks.

**[DECISION] `execute_trade` skill requires literal `"YES"` confirmation**  
Rationale (C1 from PRD): Market orders cannot be recalled once filled. The cost of a confirmation prompt is seconds; the cost of an unintended order is potentially thousands of dollars and regulatory exposure. The confirmation gate is enforced at the code level and will be verified by a CI unit test — not just documented in a readme that could be ignored.

**[DECISION] Append-only audit log (C3 from PRD)**  
Rationale: Regulatory compliance and investor trust both require that the signal → order → fill ledger cannot be retroactively altered. A mutable audit log is legally worthless. The append-only constraint is enforced at the database level (PostgreSQL RULE) not just at the application level.

**[DECISION] Paper trading phase gate of 4 weeks minimum before live capital (C8 from PRD)**  
Rationale: Backtests can be overfitted or contain subtle look-ahead bias that only surfaces in real-time operation. Four weeks of paper trading with zero critical incidents is the minimum evidence that the live system behaves as expected. "Critical incident" is defined precisely in the PRD so this gate cannot be gamed by redefining what counts as a problem.

**[DECISION] TimescaleDB as the primary time-series store**  
Rationale: PostgreSQL-compatible (lowers cognitive overhead; team knows SQL), excellent time-series compression, supports point-in-time queries natively, mature ecosystem. Alternative was InfluxDB — rejected because its query language (Flux) adds a learning curve and its financial data ecosystem is thinner.

**[DECISION] CVXPY as the optimization engine**  
Rationale: Declarative constraint syntax maps cleanly to portfolio constraint specifications (sector limits, factor exposure bounds, position limits). Switching optimization objectives (MVO → risk parity → max Sharpe) is a matter of rewriting the objective expression, not restructuring the code. Alternative was scipy.optimize — rejected because constraint declaration is more verbose and error-prone.

**[DECISION] MLflow for experiment tracking**  
Rationale: Every backtest run must be reproducible from its run ID. MLflow logs params, metrics, and artifacts together; links strategy config hashes to results; provides a UI for comparing runs. Alternative was Weights & Biases — rejected to avoid a SaaS dependency for a financial system where data residency matters.

**[DECISION] DVC for data versioning**  
Rationale: Pinning a backtest to a specific dataset snapshot (C7 in PRD) requires a versioning tool that treats data files as first-class versioned artifacts. DVC integrates with git so a git commit hash + DVC data hash together fully specify a reproducible environment.

---

#### Files created this session

| File | Purpose | Size |
|------|---------|------|
| `PRD.md` | Full Product Requirements Document | 921 lines |
| `Worklog.md` | This file — running engineering journal | — |
| `CLAUDE.md` | Project context for Claude Code sessions | — |

---

#### What was NOT done (and why)

- **No code written yet.** Per the PRD and the phase-gate design, the correct sequence is: PRD → CLAUDE.md + Worklog → skeleton folder structure → data layer code. Writing application code before the project context documents are in place would mean future sessions lack orientation.
- **No infrastructure provisioned.** Docker Compose, TimescaleDB, and MinIO setup is Phase 1 work (Weeks 1–2). Premature infrastructure means unmaintained scaffolding.

---

#### Next steps (Phase 1, Weeks 1–2)

1. Create the full folder skeleton (all directories from `PRD.md` Section 5, with `.gitkeep` files)
2. Write `docker-compose.yml` for TimescaleDB + MinIO + Redis + Airflow local stack
3. Write database schema SQL for `market` tables (OHLCV, corporate actions)
4. Write the Polygon.io OHLCV ingestion client with quality checks
5. Write Alembic migration setup

---

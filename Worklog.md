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

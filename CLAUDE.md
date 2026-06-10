# RQIS — Claude Code Project Context

This file is the first thing every Claude Code session should read. It gives you orientation so you can make correct decisions without repeatedly re-reading the entire PRD.

---

## Confirmed build configuration (operator decisions 2026-06-05)

| Topic | Decision |
|-------|----------|
| Market data (Phase 1) | **yfinance** (free) — Polygon.io deferred to Phase 2+ |
| Infrastructure | **Docker Compose** on local machine — cloud migration deferred |
| Broker | **IBKR only** — no Alpaca. Paper = port 7497, Live = port 7496 |
| Team | **Solo** — clean interfaces designed for future handoff, no multi-user auth |

See `Worklog.md` Session 2 for full rationale.

---

## What this project is

**Robust Quant Investment System (RQIS)** — a production-grade quantitative investment platform covering the full alpha-generation lifecycle:

```
Data → Signals → Portfolio Construction → Execution → Risk Monitoring → Reporting
```

It is built to eventually trade real capital. Every decision you make must treat that seriously.

**Full requirements:** See `PRD.md`.  
**Engineering history:** See `Worklog.md` — read recent entries first.

---

## Current project phase

| Phase | Name | Status | Target completion |
|-------|------|--------|-------------------|
| 0 | PRD + Project Setup | **Complete** | Week 1 |
| 1 | Data Foundation | **Complete** | Week 6 |
| 2 | Signal Library | **Complete** | Week 12 |
| 3 | Backtesting Engine | **Next** | Week 18 |
| 4 | Portfolio + Paper Trading | Not started | Week 26 |
| 5 | Reporting + Live Trading | Not started | Week 36 |

**Active branch:** `phase-2` (Phase 2 closeout; create the Phase 3 branch before
starting backtesting implementation)

**What this means for you:** Phase 2 is complete. The point-in-time-safe signal
research workflow is operational, momentum is the only currently accepted
production factor, and rejected candidate factors remain diagnostic-only.
Phase 3 backtesting is next. No broker connections exist yet and no real capital
is at risk. Safety constraints C1–C9 still apply as design rules.

---

## Critical safety rules — read every session

These are non-negotiable. If you are ever about to violate one, stop and ask the operator.

| # | Rule | What to do if you're about to violate it |
|---|------|------------------------------------------|
| C1 | Never submit a broker order without displaying the full order list and receiving a literal `"YES"` from the operator | Stop. Display the order list. Ask for confirmation. |
| C2 | Never run raw `ALTER TABLE` / `DROP TABLE` against a DB — always use Alembic migrations | Write a migration file instead. |
| C3 | Never UPDATE or DELETE from audit log tables | Append a correction record with a `correction_of` foreign key instead. |
| C4 | Never reset the circuit breaker automatically — only a human with a reason code may do it | Stop. Tell the operator the circuit breaker is open. Ask them to reset manually. |
| C5 | Never put API keys, secrets, or passwords in source code, committed config files, or logs | Use `.env` (gitignored) or Vault. Reference `os.environ` in code. |
| C6 | Never modify a strategy config YAML that has been used in a live session — create a new version | Create `v{N+1}_{description}.yaml`. |
| C7 | Never run a backtest without recording the data snapshot version in MLflow | Pass `data_version` to the backtest call. |
| C8 | Never switch from paper trading to live capital without a 4-week clean paper-trading run | This is a phase gate. Escalate to the operator. |
| C9 | Before any destructive infrastructure action (drop table, delete from object storage, cancel all orders, switch to live), display the action and require `"YES"` | Display → confirm → act. |

---

## Repository layout

```
rqis/
├── .claude/skills/        ← Claude Code skill definitions (11 skills)
├── data/                  ← Data ingestion, normalization, storage
├── signals/               ← Factor library, signal research, scoring
├── portfolio/             ← Optimization, risk model, rebalancing
├── execution/             ← OMS, brokers, algos, transaction costs
├── risk/                  ← Real-time risk, alerts, circuit breaker
├── backtesting/           ← Event-driven engine, walk-forward, attribution
├── reporting/             ← Tearsheets, audit trail, dashboards
├── mcp_servers/           ← MCP servers exposing RQIS APIs to Claude
├── airflow/               ← DAGs for daily pipeline and rebalancing
├── infra/                 ← Docker, DB migrations, Vault policies
├── config/                ← Settings YAML, universe definition, strategy configs
├── notebooks/             ← Research notebooks (NEVER imported in production)
├── tests/                 ← Integration tests
└── docs/                  ← Architecture, data dictionary, runbooks, API docs
```

Full directory tree with file-level detail: `PRD.md` Section 5.

---

## Environment variables (never hardcode these)

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | TimescaleDB connection string |
| `REDIS_URL` | Redis connection string |
| `MINIO_ENDPOINT` | Object storage endpoint |
| `MINIO_ACCESS_KEY` | Object storage access key |
| `MINIO_SECRET_KEY` | Object storage secret key |
| `POLYGON_API_KEY` | Polygon.io market data API key (Phase 2+) |
| `IBKR_HOST` | IBKR TWS/Gateway host (default: `127.0.0.1`) |
| `IBKR_PORT` | `7497` = paper trading, `7496` = live trading — **never hardcode** |
| `MLFLOW_TRACKING_URI` | MLflow tracking server URL |
| `PAPER_TRADING` | `"true"` or `"false"` — NEVER change from `"false"` without operator `"YES"` confirmation (C9) |

Reference `.env.example` for the full list.

---

## Conventions

### Dates and times
- All stored timestamps are UTC.
- "As-of date" for point-in-time queries means: data whose `release_date ≤ as_of_date`.
- Never use `datetime.now()` in backtesting code — always use the simulation clock.
- Date columns: `DATE` type for calendar dates; `TIMESTAMPTZ` for event timestamps.

### Units
- Prices: USD, stored as `NUMERIC(18, 6)` (not float — avoids rounding errors in financial math).
- Returns: decimal form (0.05 = 5%), never percentage form in the DB or code.
- Weights: decimal form summing to 1.0.
- Volatility: annualized decimal form.

### Naming
- Database tables: `snake_case`, plural nouns (`daily_prices`, `signal_scores`).
- Python modules: `snake_case`.
- Strategy config files: `v{N}_{short_description}.yaml` (e.g., `v1_base_momentum.yaml`).
- MLflow experiments: `{strategy_name}/{signal_group}` (e.g., `base_momentum/value`).

### Code style
- Type hints required on all public functions.
- No bare `except:` — always catch specific exceptions.
- No `print()` in library code — use `structlog.get_logger()`.
- Tests live in `tests/` subdirectories mirroring the module structure they test.

---

## MCP servers (not yet built — Phase 0)

When built, these servers expose RQIS APIs to Claude skills:

| Server | Module | Key tools |
|--------|--------|-----------|
| `data_server` | `mcp_servers/data_server.py` | `fetch_ohlcv`, `fetch_fundamentals`, `get_universe` |
| `backtest_server` | `mcp_servers/backtest_server.py` | `run_backtest`, `get_backtest_results` |
| `risk_server` | `mcp_servers/risk_server.py` | `get_portfolio_risk`, `get_factor_exposures`, `get_var` |

---

## The 11 Claude skills

Defined in `.claude/skills/*.md` (not yet created — Phase 0).

| Skill | Safe to run autonomously? | Touches broker? |
|-------|--------------------------|-----------------|
| `data_fetch` | Yes | No |
| `signal_research` | Yes | No |
| `screen` | Yes | No |
| `score` | Yes | No |
| `portfolio_construct` | Yes (output is STAGED orders only) | No |
| `risk_check` | Yes | No |
| `execute_trade` | **No — requires `"YES"` confirmation** | **Yes** |
| `backtest` | Yes | No |
| `attribute` | Yes | No |
| `report` | Yes | No |
| `monitor` | Yes (read-only) | No |

---

## Worklog discipline

**Every session must append to `Worklog.md`** before ending.

Minimum entry content:
1. Date, session number, operator
2. Branch and commits made
3. What was done (bullet list)
4. Any `[DECISION]` records with rationale
5. Any `[BLOCKER]` or `[SAFETY]` flags
6. Next steps

If you made a significant architectural or implementation decision, record it in the Worklog with a `[DECISION]` tag and a rationale. Future sessions (and human reviewers) will thank you.

---

## Key documents

| Document | Purpose |
|----------|---------|
| `PRD.md` | Full requirements, all features, milestones, success criteria, constraints |
| `Worklog.md` | Running engineering journal — what was done, when, and why |
| `CLAUDE.md` | This file — project orientation for Claude Code |
| `docs/architecture.md` | (to be created) System diagram and data flows |
| `docs/data_dictionary.md` | (to be created) Every table, column, unit, source |
| `docs/runbooks/` | Operational procedures (e.g. `airflow_fire_drill.md`) |

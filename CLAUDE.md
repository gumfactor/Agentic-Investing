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
| 3 | Backtesting Engine | **Live validation complete** | Week 18 |
| 4 | Portfolio + Paper Trading | **Implementation complete — paper run pending** | Week 26 |
| 5 | Reporting + Live Trading | Not started | Week 36 |

**Active branch:** `local/linking-to-IBKR`

**What this means for you:** Phase 4 implementation is complete as of 2026-06-15.
All three module trees (portfolio/, execution/, risk/) are now fully implemented:

- **portfolio/optimization/**: MVO (max-Sharpe/min-variance) + Risk-Parity (Spinu 2013)
- **portfolio/risk_model/**: Ledoit-Wolf covariance + PortfolioConstraints
- **portfolio/rebalancing/**: Calendar + drift trigger
- **execution/oms/**: Order state machine (STAGED→FILLED) + ComplianceEngine
- **execution/brokers/**: IBKRBroker (paper port 7497 / live port 7496)
- **execution/cost_model/**: Almgren-Chriss cost estimator
- **risk/realtime/**: Historical/parametric VaR, CVaR, beta, RiskMonitor
- **risk/alerts/**: AlertManager with pluggable dispatch
- **risk/circuit_breaker.py**: CLOSED/OPEN state machine (C4-compliant)

Three Claude skills added: `portfolio_construct` (safe), `risk_check` (safe),
`execute_trade` (requires "YES" — C1).

**Last recorded validation:** 536 non-integration tests passed in Session 20.

Exit criterion for Phase 4: 4 consecutive weeks of paper trading with zero
critical bugs; circuit breaker fire-drill test. Paper trading has NOT yet begun.
No real capital is at risk. The IBKR paper account connection (port 7497)
has been smoke-tested, including a Canadian CAD NAV account with USD-equivalent
NAV conversion.

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
| `IBKR_FX_RATE_CAD_USD` | Optional explicit CAD→USD fallback when IBKR FX market data is unavailable; requires fresh `IBKR_FX_RATE_CAD_USD_AS_OF` |
| `IBKR_FX_RATE_CAD_USD_AS_OF` | Required `YYYY-MM-DD` date for manual CAD→USD fallback; stale dates are rejected |
| `MLFLOW_TRACKING_URI` | MLflow tracking server URL |
| `PAPER_TRADING` | `"true"` or `"false"` — NEVER change from `"false"` without operator `"YES"` confirmation (C9) |

Reference `.env.example` for the full list.

---

## Phase 4 paper readiness command

Run this before any daily paper-trading workflow:

```powershell
$env:PAPER_TRADING='true'
$env:IBKR_PORT='7497'
$env:IBKR_FX_RATE_CAD_USD='<current CAD-to-USD rate>'
$env:IBKR_FX_RATE_CAD_USD_AS_OF=(Get-Date -Format yyyy-MM-dd)
python -m scripts.paper_readiness_check
```

The command is read-only. It requires explicit `PAPER_TRADING=true` and
`IBKR_PORT=7497`, verifies no live-run clearance flag, checks TWS/Gateway socket
reachability, confirms IBKR paper broker connection, and reads current
positions, NAV by currency, and finite positive USD-equivalent NAV. It does not
generate, stage, submit, cancel, or reconcile orders.

## Phase 4 paper input command

Run this after paper readiness and before portfolio construction:

```powershell
python -m scripts.paper_inputs_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum
```

The command is read-only. It loads the strategy config, latest `daily_prices`,
and latest strategy-specific `alpha_scores` from `DATABASE_URL`, then verifies
recency, finite alpha scores, positive latest closes, and enough scored tickers
with latest prices for `portfolio.n_long`. The `--strategy-id` argument is
required explicitly because stored score IDs can differ from display names and
YAML versions. It does not connect to IBKR, build target weights, stage orders,
submit orders, cancel orders, or reconcile fills.

Current live status as of 2026-06-20: `daily_prices` is recent through
2026-06-18, but `alpha_scores` is stale for paper trading. `v1` scores stop at
2024-12-31; `v1_base_momentum` scores stop at 2026-06-09. Refresh the daily
signal pipeline before proceeding to portfolio construction.

## Phase 4 paper target command

Run this after the paper input command passes:

```powershell
python -m scripts.paper_target_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum
```

The command is read-only. For the current `equal_weight` v1 strategy, it selects
the top `portfolio.n_long` alpha scores, applies `portfolio.max_position_weight`,
prints target weights, and leaves any cap-bound residual in cash. It does not
read IBKR positions, generate order candidates, stage orders, submit orders,
cancel orders, or reconcile fills. Unsupported portfolio methods fail closed
until their paper-target semantics are explicitly implemented and reviewed.

## Phase 4 paper order candidate command

Run this after the paper target command passes, using an explicit local snapshot
of current cash and positions:

```powershell
python -m scripts.paper_order_candidates_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum --portfolio-input .\local\paper_portfolio_snapshot.json
```

The portfolio snapshot is a local JSON object:

```json
{
  "as_of": "2026-06-20",
  "cash": 1000.0,
  "positions": [
    {"ticker": "AAPL", "quantity": 5.0, "price": 200.0}
  ]
}
```

The command is read-only and staging-free. It reuses the Step 3 target-weight
gate, computes current weights from the local snapshot, then prints deterministic
SELL-before-BUY order candidates with estimated shares and notional. It requires
explicit `--strategy-id`, finite positive NAV/prices, non-negative cash and
quantities, unique tickers, a fresh `as_of` date, and skips deltas below
`--min-delta-weight`. It does not connect to IBKR, instantiate OMS orders, run
compliance or risk gates, stage orders, submit orders, cancel orders, reconcile
fills, or require human `YES`.

Current live status as of 2026-06-20: this command is expected to fail closed
through the Step 3 target gate until stale `alpha_scores` are refreshed.

## Phase 4 paper risk/compliance command

Run this after the paper order candidate command passes, using the same explicit
local snapshot of current cash and positions:

```powershell
python -m scripts.paper_risk_compliance_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum --portfolio-input .\local\paper_portfolio_snapshot.json
```

The command is a read-only preflight. It reuses the Step 4 order-candidate path,
then validates candidate schema, finite weights/notionals/shares, long-only
targets by default unless the strategy config or `--allow-shorts` explicitly
allows shorts, sell quantities against the local snapshot, per-position
concentration, max gross target weight, optional turnover, and an in-memory
`ComplianceEngine` adapter with `circuit_breaker_open=False` supplied
explicitly. This is a data-only adapter: live circuit-breaker state, wash-sale
history, and sector maps are not inspected unless supplied as local context in
a future slice. The adapter creates transient OMS `Order` DTOs only for
in-memory `ComplianceEngine.check()` calls; they are never registered with
`OrderManager.stage()`. It never connects to IBKR, instantiates `OrderManager`,
stages orders, submits/cancels/reconciles broker orders, resets/trips live
circuit breakers, or asks for/consumes human `YES`.

Useful local-only overrides:

```powershell
python -m scripts.paper_risk_compliance_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum --portfolio-input .\local\paper_portfolio_snapshot.json --max-turnover-weight 0.25 --max-gross-target-weight 1.0
```

Current live status as of 2026-06-20: this command is expected to fail closed
through the reused Step 3/4 path until stale `alpha_scores` are refreshed. It
also depends on the operator-provided local snapshot being fresh and accurate;
it does not verify broker state directly.

## Phase 4 paper stage-only blotter command

Run this after the paper risk/compliance command passes, using the same explicit
local snapshot of current cash and positions:

```powershell
python -m scripts.paper_stage_blotter_check --strategy-config config\strategy\v1_base_momentum.yaml --strategy-id v1_base_momentum --portfolio-input .\local\paper_portfolio_snapshot.json --output .\local\paper_stage_blotter.json
```

The command creates a local JSON blotter artifact for operator review only. It
reuses the Step 5 risk/compliance pass path and writes the artifact only after
those gates pass. The artifact includes `schema_version`,
`artifact_type=paper_stage_only_order_blotter`, `run_id`,
`generated_at_utc`, `paper_only=true`, `stage_only=true`, source target and
snapshot dates, candidate rows, a risk/compliance summary, and a
`candidate_rows_sha256` checksum for later reconciliation. It also records
`strategy_config_sha256`, `portfolio_input_sha256`, a gate-input checksum, and
an artifact-level checksum. Existing output paths fail closed with an atomic
no-clobber write unless `--overwrite` is passed.

Step 6 uses plain local artifact rows, not live OMS registration. It never
connects to IBKR, instantiates `OrderManager`, registers staged OMS orders,
submits/cancels/reconciles broker orders, resets/trips live circuit breakers,
or asks for/consumes human `YES`. It also rejects `PAPER_RUN_CLEARED=true`
because that is a live-trading clearance flag. The only OMS `Order` DTOs involved are the
transient Step 5 data-only `ComplianceEngine.check()` adapters inherited from
the risk/compliance preflight; they are not written to the blotter and are
never registered with a live or in-memory `OrderManager`.

Current live status as of 2026-06-20: this command is expected to fail closed
through the reused Step 3/4/5 path until stale `alpha_scores` are refreshed. It
also depends on the operator-provided local snapshot being fresh and accurate;
it does not verify broker state directly.

## Phase 4 paper submit/reconcile preflight command

Run this after the stage-only blotter artifact has been generated and reviewed:

```powershell
python -m scripts.paper_submit_reconcile_check --blotter .\local\paper_stage_blotter.json
```

The default mode is dry-run validation/display only. It requires
`PAPER_TRADING=true`, `IBKR_PORT=7497`, and `PAPER_RUN_CLEARED` unset or false
even for dry-run. It revalidates the Step 6 artifact before any broker attempt:
schema/version, `paper_only=true`, `stage_only=true`, pre-submission safety
flags, source/provenance checksums, artifact checksum, candidate row checksum,
operator review rows, and absence of broker IDs or submitted/reconciled
statuses. It prints the full order list for C1 review and then stops without
connecting to IBKR.

Actual paper submission requires the operator to inspect that displayed list
and pass the literal confirmation plus the exact reviewed blotter checksum:

```powershell
$reviewed = (Get-FileHash .\local\paper_stage_blotter.json -Algorithm SHA256).Hash.ToLower()
python -m scripts.paper_submit_reconcile_check --blotter .\local\paper_stage_blotter.json --confirm YES --reviewed-blotter-sha256 $reviewed --output .\local\paper_submit_reconciliation.json
```

The submission path still refuses live port `7496`, refuses
`PAPER_RUN_CLEARED=true`, verifies the broker adapter reports paper mode before
and after connection, submits limit orders using the Step 6 reference prices,
polls once for initial fill state, disconnects, and writes a separate local
reconciliation artifact with broker response details and checksums. The
reconciliation artifact is created before broker submission and updated after
each accepted broker response, so partial failures still leave an audit record.
It never modifies the Step 6 blotter in place, never cancels orders, never
resets/trips circuit breakers, and never supports live orders.

Current live status as of 2026-06-20: Step 7 remains operationally blocked
until the upstream stale `alpha_scores` blocker is cleared and a fresh Step 6
blotter can be generated from current inputs. Automated tests use fake broker
adapters only; do not point tests at a real IBKR session.

## Phase 4 paper run audit record command

Run this as the final preflight/run-record slice after Step 6, and include the
Step 7 reconciliation artifact when paper submission was actually attempted:

```powershell
python -m scripts.paper_run_audit_check --blotter .\local\paper_stage_blotter.json --status BLOCKED --blocker "alpha_scores are stale for paper trading" --output .\local\paper_run_audit.json
```

With a successful Step 7 reconciliation:

```powershell
python -m scripts.paper_run_audit_check --blotter .\local\paper_stage_blotter.json --reconciliation .\local\paper_submit_reconciliation.json --status SUBMITTED --step1-status PASS --step2-status PASS --step3-status PASS --step4-status PASS --step5-status PASS --output .\local\paper_run_audit.json
```

The command writes a separate local JSON audit/run record for phase-gate review.
It validates the Step 6 blotter schema, provenance checksums, candidate checksum,
and artifact checksum, and validates the Step 7 reconciliation schema/checksums
when supplied. The audit artifact records `schema_version`, `run_id`,
`generated_at_utc`, `paper_only=true`, operator-visible status, gate statuses,
artifact paths, file hashes, git branch/commit/dirty flag, command/schema
versions, validation summary, unresolved blockers, safety assertions, and next
action. Existing output paths fail closed with an atomic no-clobber write unless
`--overwrite` is passed.

Use `BLOCKED` for pre-submission blockers such as stale inputs. Use `FAILED`
only when Step 7 attempted paper submission and produced a failed reconciliation
artifact; the command requires that artifact for `FAILED`.

Step 8 is read-only with respect to trading systems and prior artifacts. It
never connects to IBKR, submits/cancels/reconciles broker orders, mutates the
Step 6 blotter or Step 7 reconciliation artifact, resets/trips circuit breakers,
or asks for/consumes human `YES`.

Current live status as of 2026-06-20: this command can record the current
blocked state once a valid Step 6 blotter exists, but upstream paper execution
remains blocked until stale `alpha_scores` are refreshed.

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
- Database tables: `snake_case`, plural nouns (`daily_prices`, `alpha_scores`).
- Python modules: `snake_case`.
- Strategy config files: `v{N}_{short_description}.yaml` (e.g., `v1_base_momentum.yaml`).
- MLflow experiments: `{strategy_name}/{signal_group}` (e.g., `base_momentum/value`).

### Code style
- Type hints required on all public functions.
- No bare `except:` — always catch specific exceptions.
- No `print()` in library code — use `structlog.get_logger()`.
- Tests live in `tests/` subdirectories mirroring the module structure they test.

### Phase 3 operational commands

Pin a complete backtest bundle after a successful score backfill:

```powershell
python -m scripts.pin_snapshot --strategy-id v1 --benchmark SPY
```

The command pins `daily_prices`, `alpha_scores`, `corporate_actions`, and the
SPY benchmark under one snapshot date, then writes a dataset manifest. Use the
manifest path, not a prices-only object path, as the MLflow `data_version`.

Current validated bundle:

`rqis-snapshots/manifests/2026-06-14/manifest.json`

Current supported backtest window:

`2022-07-11` through `2024-12-31`

The older `2020-01-02` start requires additional price ingestion beginning
roughly in late 2018 so the 273-trading-day momentum lookback is available.

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

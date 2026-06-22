# Product Requirements Document
## Robust Quant Investment System (RQIS)

**Version:** 1.0  
**Status:** Draft  
**Owner:** Engineering  
**Last Updated:** 2026-06-05

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals](#2-goals)
3. [Scope](#3-scope)
4. [Tech Stack](#4-tech-stack)
5. [Folder Structure](#5-folder-structure)
6. [Features](#6-features)
7. [Milestones](#7-milestones)
8. [Success Criteria](#8-success-criteria)
9. [Telemetry](#9-telemetry)
10. [Documentation](#10-documentation)
11. [Safety & Reversibility Constraints](#11-safety--reversibility-constraints)

---

## 1. Executive Summary

The Robust Quant Investment System (RQIS) is a production-grade, modular quantitative investment platform. It covers the full alpha-generation lifecycle: data ingestion → signal research → portfolio construction → execution → risk monitoring → reporting. The system is designed to be operated by a small team of quantitative researchers and engineers, with Claude Code skills acting as the primary automation layer for research, simulation, and live operations.

The system prioritizes:
- **Correctness over speed** — point-in-time data integrity, look-ahead-bias prevention, reproducibility
- **Safety over convenience** — all live-order actions require explicit human confirmation; destructive changes are staged and reversible
- **Modularity** — each layer is independently testable, replaceable, and versionable

---

## 2. Goals

### Primary Goals

| # | Goal | Rationale |
|---|------|-----------|
| G1 | Build a complete, production-grade quant research and execution stack | End-to-end coverage removes data silos and enables fully auditable signal-to-fill traceability |
| G2 | Ensure point-in-time correctness across all data and backtests | Eliminates look-ahead bias — the single most common source of false alpha |
| G3 | Provide real-time risk monitoring with hard circuit breakers | Protects capital; ensures no position or drawdown limit can be silently breached |
| G4 | Make every trade decision fully auditable | Regulatory compliance, investor confidence, and debugging require a complete signal → order → fill ledger |
| G5 | Automate research iteration without automating irreversible actions | Claude skills accelerate hypothesis testing; humans approve all live-order submissions |

### Secondary Goals

- Reduce time from signal hypothesis to validated backtest to under 48 hours
- Support multi-asset classes: equities (initial), options, futures (later phases)
- Support pluggable broker backends so switching execution venue is a config change
- Provide institutional-quality tearsheets consumable by non-technical stakeholders

---

## 3. Scope

### In Scope (v1.0)

- US equity universe (S&P 1500 + ADRs as default; configurable)
- Daily and intraday (1-min bar) historical data
- Factor-based signal library: value, momentum, quality, low-volatility, growth, sentiment
- Mean-variance and risk-parity portfolio optimization
- Paper trading and live trading via IBKR API (Alpaca deferred — IBKR only per Session 2 decision)
- Event-driven backtesting engine with realistic fills and transaction costs
- Real-time risk dashboard and breach alerting
- Automated tearsheet generation (daily/weekly/monthly)
- Claude Code skill layer for research, screening, scoring, execution, and reporting

### Out of Scope (v1.0 — future phases)

- Crypto assets
- Fixed income / rates
- High-frequency (sub-second) execution
- Proprietary exchange co-location
- Direct prime brokerage connectivity (FIX/FAST protocols)
- Multi-manager fund accounting

---

## 4. Tech Stack

### Core Language & Runtime

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Primary language | Python 3.12 | Ecosystem depth for quant (pandas, scipy, cvxpy); team familiarity |
| Secondary language | Rust (optional, hot paths) | Backtesting event loop and order book simulation if Python bottlenecks |
| Runtime | CPython + optional Cython compilation | Baseline correctness; optimize only after profiling |

### Data Layer

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Time-series DB | TimescaleDB (PostgreSQL extension) | SQL familiarity, excellent time-series compression, point-in-time query support |
| Document store | PostgreSQL JSONB | Store raw API responses, filings, transcripts alongside structured tables |
| Object storage | MinIO (self-hosted S3-compatible) | Versioned parquet snapshots for reproducible backtests; swap for AWS S3 in cloud deployment |
| Cache | Redis | Real-time signal scores, intraday price snapshots, session state |
| Data versioning | DVC (Data Version Control) | Pin dataset versions to strategy configs so backtests are fully reproducible |

### Signal & Research

| Component | Choice | Rationale |
|-----------|--------|-----------|
| DataFrame engine | pandas + polars | pandas for familiarity; polars for large cross-sectional sorts |
| Statistical computing | scipy, statsmodels | Hypothesis testing, regression, factor validation |
| ML/alpha modeling | scikit-learn, LightGBM | Feature engineering, signal combination; no deep learning in v1 |
| Experiment tracking | MLflow | Log every backtest run: params, metrics, artifacts |
| NLP / sentiment | spaCy + FinBERT | News and transcript sentiment scoring |

### Portfolio & Risk

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Optimizer | CVXPY | Convex optimization with constraint declarations; supports MVO, risk-parity |
| Risk model | PyPortfolioOpt + custom Ledoit-Wolf | Covariance shrinkage; factor risk decomposition |
| Greeks / options | py_vollib | Black-Scholes, implied vol, Greeks for options positions |

### Execution

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Broker — IBKR | ib_insync | Async IBKR API wrapper — sole production broker (paper port 7497 / live port 7496) |
| Broker — Alpaca | alpaca-trade-api | **Deferred** — dropped in Session 2; IBKR handles both paper and live |
| Order management | Custom OMS (see features) | Full state machine; staged→pending→live→filled→cancelled |

### Infrastructure

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Orchestration | Apache Airflow | DAG-based scheduling for daily data pipelines, signal runs, rebalances |
| Containerization | Docker + Docker Compose | Reproducible local dev; migrate to Kubernetes in cloud phase |
| Secrets management | HashiCorp Vault (dev: .env + dotenv-vault) | API keys, broker credentials never in source |
| Monitoring | Prometheus + Grafana | System metrics, lag monitoring, alert rules |
| Logging | structlog → Loki | JSON structured logs; queryable via Grafana Loki |
| CI/CD | GitHub Actions | Lint, test, type-check on every PR; no auto-deploy to live |

### Claude Code Integration

| Component | Choice |
|-----------|--------|
| Skill runtime | Claude Code CLI (skills defined in `.claude/skills/`) |
| MCP servers | Custom MCP servers exposing data, backtest, and risk APIs to skills |
| LLM model | claude-sonnet-4-6 (default); escalate to Opus for complex reasoning |

---

## 5. Folder Structure

```
rqis/
├── .claude/
│   ├── settings.json              # Claude Code permissions and hooks
│   └── skills/                    # Claude Code skill definitions
│       ├── data_fetch.md
│       ├── signal_research.md
│       ├── screen.md
│       ├── score.md
│       ├── portfolio_construct.md
│       ├── risk_check.md
│       ├── execute_trade.md
│       ├── backtest.md
│       ├── attribute.md
│       ├── report.md
│       └── monitor.md
│
├── data/                          # Data layer
│   ├── ingestion/
│   │   ├── market/                # Price/volume feeds
│   │   │   ├── polygon_client.py
│   │   │   ├── yfinance_client.py
│   │   │   └── ib_data_client.py
│   │   ├── fundamental/           # Financials, estimates, filings
│   │   │   ├── simfin_client.py
│   │   │   └── sec_edgar_client.py
│   │   └── alternative/           # Sentiment, macro, insider
│   │       ├── news_nlp.py
│   │       ├── fred_client.py
│   │       └── insider_client.py
│   ├── normalization/
│   │   ├── corporate_actions.py   # Split/dividend adjustments
│   │   ├── point_in_time.py       # PIT-correct data joins
│   │   └── quality_checks.py      # Anomaly detection, completeness
│   ├── storage/
│   │   ├── timescale_writer.py
│   │   ├── parquet_snapshots.py
│   │   └── schema/
│   │       ├── market.sql
│   │       ├── fundamentals.sql
│   │       └── signals.sql
│   └── tests/
│
├── signals/                       # Signal generation layer
│   ├── factors/
│   │   ├── value.py               # P/E, P/B, EV/EBITDA, etc.
│   │   ├── momentum.py            # Price momentum, earnings momentum
│   │   ├── quality.py             # ROE, ROIC, accruals, leverage
│   │   ├── low_vol.py             # Beta, realized vol, idiosyncratic vol
│   │   ├── growth.py              # Revenue/earnings growth, estimate revisions (Phase 5)
│   │   ├── sentiment.py           # NLP scores, short interest, insider flow (Phase 5)
│   │   └── technical.py           # MA crossovers, RSI, MACD, ATR, breakout (Phase 5)
│   ├── research/
│   │   ├── hypothesis_template.py # Standard research notebook template
│   │   ├── statistical_tests.py   # IC, t-stats, multiple testing correction
│   │   └── decay_analysis.py      # Signal half-life measurement
│   ├── scoring/
│   │   ├── normalizer.py          # Cross-sectional z-score, rank normalization
│   │   ├── combiner.py            # Composite score construction
│   │   └── universe.py            # Security eligibility filters
│   └── tests/
│
├── portfolio/                     # Portfolio construction layer
│   ├── optimization/
│   │   ├── mean_variance.py
│   │   ├── risk_parity.py
│   │   └── constraints.py         # Sector, factor, position, ESG limits
│   ├── risk_model/
│   │   ├── covariance.py          # Ledoit-Wolf, factor model
│   │   └── factor_exposures.py
│   ├── rebalancing/
│   │   ├── turnover_control.py    # Cost-aware threshold triggers
│   │   └── sizing.py              # Kelly, vol-target, equal-weight
│   └── tests/
│
├── execution/                     # Execution layer
│   ├── oms/
│   │   ├── order.py               # Order dataclass + state machine
│   │   ├── order_manager.py       # Staging, routing, cancellation
│   │   ├── compliance.py          # Pre-trade checks (wash sale, concentration)
│   │   └── trade_history.py       # Append-only fill store for P&L + wash-sale history (Phase 5)
│   ├── brokers/
│   │   ├── base_broker.py         # Abstract interface
│   │   └── ibkr_broker.py         # sole production broker (Alpaca deferred)
│   ├── algos/
│   │   ├── twap.py
│   │   ├── vwap.py
│   │   └── smart_router.py
│   ├── cost_model/
│   │   └── transaction_costs.py   # Bid-ask, market impact, slippage
│   └── tests/
│
├── risk/                          # Risk & monitoring layer
│   ├── realtime/
│   │   ├── var_calculator.py      # Historical and parametric VaR
│   │   ├── drawdown_monitor.py
│   │   └── factor_exposure_monitor.py
│   ├── stress/
│   │   └── scenario_engine.py     # 2008, COVID, rate shock scenarios
│   ├── liquidity/
│   │   └── liquidity_risk.py      # Days-to-liquidate, concentration
│   ├── alerts/
│   │   ├── alert_manager.py       # Breach detection + notification routing
│   │   └── circuit_breaker.py     # Hard stops: halt all orders
│   └── tests/
│
├── backtesting/                   # Backtesting & research infrastructure
│   ├── engine/
│   │   ├── event_loop.py          # Event-driven simulation core
│   │   ├── fill_simulator.py      # Realistic order fills with slippage
│   │   └── data_handler.py        # Point-in-time data access during sim
│   ├── validation/
│   │   ├── walk_forward.py        # Walk-forward out-of-sample testing
│   │   └── overfitting_checks.py  # Multiple-testing correction, deflated Sharpe
│   ├── attribution/
│   │   ├── brinson.py             # Brinson-Hood-Beebower attribution
│   │   └── factor_decomposition.py
│   ├── experiment_tracking/
│   │   └── mlflow_logger.py       # Log params, metrics, artifacts
│   └── tests/
│
├── reporting/                     # Reporting & oversight layer
│   ├── tearsheets/
│   │   ├── performance.py         # Returns, Sharpe, drawdown, benchmark-rel
│   │   └── factor_exposure.py     # What risks am I running?
│   ├── audit/
│   │   ├── trade_log.py           # Signal → order → fill lineage
│   │   └── paper_operational_ledger.py  # Append-only paper-run JSONL ledger
│   ├── dashboards/
│   │   ├── grafana/               # Dashboard JSON configs
│   │   └── streamlit_app.py       # Internal stakeholder dashboard
│   └── tests/
│
├── mcp_servers/                   # MCP servers exposing RQIS APIs to Claude
│   ├── data_server.py
│   ├── backtest_server.py
│   └── risk_server.py
│
├── airflow/                       # Orchestration DAGs
│   ├── dags/
│   │   ├── daily_data_pipeline.py
│   │   ├── signal_scoring.py
│   │   └── rebalance_trigger.py
│   └── plugins/
│
├── infra/                         # Infrastructure as code
│   ├── docker/
│   │   ├── docker-compose.yml
│   │   └── Dockerfile
│   ├── db/
│   │   └── migrations/            # Alembic migration files
│   └── vault/
│       └── policies/
│
├── config/
│   ├── settings.yaml              # Environment-specific config
│   ├── universe.yaml              # Security eligibility rules
│   ├── sector_map.yaml            # Ticker → GICS sector mapping (Phase 5)
│   └── strategy/                  # Versioned strategy configs
│       ├── v1_base_momentum.yaml
│       └── v2_mvo_momentum.yaml
│
├── strategy_registry/             # Strategy catalog and activation management (Phase 5)
│   ├── registry.py                # DB-backed catalog: id, status, config path, metrics
│   ├── schema/
│   │   └── strategy_registry.sql  # Alembic-managed table
│   └── tests/
│
├── notebooks/                     # Research notebooks (never run in prod)
│   └── research/
│
├── tests/                         # Cross-module integration tests
│   ├── integration/
│   └── fixtures/
│
├── docs/
│   ├── architecture.md
│   ├── data_dictionary.md
│   ├── runbooks/
│   │   ├── daily_operations.md
│   │   ├── incident_response.md
│   │   └── adding_a_signal.md
│   └── api/                       # Auto-generated from docstrings
│
├── CLAUDE.md                      # Claude Code project context
├── PRD.md                         # This document
├── pyproject.toml
├── requirements.txt
└── .env.example
```

---

## 6. Features

### F1 — Data Layer

#### F1.1 Market Data Ingestion
- Pull OHLCV bars (daily) from yfinance (primary); Polygon.io deferred to Phase 2+ (see Session 2 decision)
- Real-time quote stream via IBKR websocket (Alpaca websocket deferred along with Alpaca broker)
- Corporate actions: splits, dividends, spinoffs — applied retroactively with full audit log
- **Point-in-time enforcement:** all historical joins use `as_of_date` parameter; no future data leaks

#### F1.2 Fundamental Data
- Income statement, balance sheet, cash flow from SimFin (free tier) or Compustat (paid)
- Earnings estimates and revisions from Alpha Vantage or Refinitiv (pluggable)
- SEC EDGAR crawler for 10-K, 10-Q, 8-K filings and earnings transcripts

#### F1.3 Alternative Data
- News sentiment: headline + body scored with FinBERT; daily aggregate per ticker
- FRED macro indicators: CPI, unemployment, yield curves, credit spreads
- Short interest from FINRA; insider transactions from SEC Form 4
- Sector/industry flow data from ETF holdings changes

#### F1.4 Data Quality
- Completeness checks: alert if >5% of universe missing for any given date
- Anomaly detection: flag price jumps >3σ for manual review before writing to DB
- Schema validation on every ingestion batch (Great Expectations or Pandera)
- All raw API responses stored in object storage before transformation (idempotent reprocessing)

---

### F2 — Signal Generation

#### F2.1 Factor Library
Each factor produces a cross-sectional z-score (or rank percentile) per security per date.

| Factor Group | Signals |
|---|---|
| Value | Forward P/E, EV/EBITDA, P/B, FCF yield |
| Momentum | 12-1 month price return, earnings momentum (SUE), estimate revision trend |
| Quality | ROIC, gross margin stability, Piotroski F-score, accruals ratio |
| Low Volatility | 63-day realized vol, 1-year beta, idiosyncratic vol |
| Growth | Revenue growth YoY, EPS growth YoY, EBITDA margin expansion |
| Sentiment | Composite NLP score, short interest change, insider buy/sell ratio |
| Technical Analysis | Moving average crossovers (SMA/EMA), RSI, MACD, Bollinger Bands, ATR, volume trends, breakout detection, relative strength vs. benchmark — all expressible as Python signal modules sharing the same factor infrastructure |

#### F2.2 Signal Research Skill (`signal_research`)
- Accepts hypothesis in natural language; Claude constructs data pull + validation plan
- Computes Information Coefficient (IC), t-statistic, and turnover for each candidate signal
- Applies Bonferroni / BHY multiple-testing correction before declaring a signal valid
- Outputs a standardized research report stored in MLflow

#### F2.3 Composite Score (`score`)
- Normalizes individual factor scores cross-sectionally
- Combines via configurable weights (equal-weight default; regression-fit optional)
- Outputs a single `alpha_score` per security per date, stored in TimescaleDB

#### F2.4 Signal Decay Analysis
- Measures IC decay over 1-, 5-, 10-, 21-, 63-day horizons
- Informs rebalancing frequency: only rebalance if net IC gain exceeds transaction costs

#### F2.5 Universe Definition (`screen`)
- Configurable in `config/universe.yaml`
- Default: US-listed common stocks, >$500M market cap, >$1M ADV, not penny stocks, not in bankruptcy
- ESG screen: optional exclusion list (firearms, tobacco, fossil fuels)

---

### F3 — Portfolio Construction

#### F3.1 Optimization Engine (`portfolio_construct`)
- **Mean-Variance Optimization:** maximize expected return for a given risk budget (CVXPY)
- **Risk Parity:** equalize marginal risk contribution across positions
- **Max Sharpe:** direct Sharpe ratio maximization
- All engines output target weight vectors, not orders — OMS computes the diff

#### F3.2 Constraint Handler
- Position limits: max 5% single name (configurable)
- Sector limits: max 25% per GICS sector (configurable)
- Factor exposure bounds: constrain active beta, size, value tilts
- ESG screens enforced as hard constraints in optimizer
- Regulatory: long-only constraint; no naked shorts in v1

#### F3.3 Risk Model
- **Covariance estimation:** Ledoit-Wolf shrinkage on rolling 252-day returns
- **Factor risk model:** decompose into market, sector, style factor exposures
- Covariance matrix is re-estimated weekly; daily updates for high-vol regimes

#### F3.4 Turnover Control
- Only rebalance when signal drift exceeds `rebalance_threshold` (default: 0.5 IC half-life)
- Transaction cost simulation runs before any rebalance is committed to confirm net benefit
- Minimum holding period: configurable per strategy (default: 5 trading days)

#### F3.5 Sizing
- **Volatility targeting:** scale position sizes so realized portfolio vol ≈ target (default: 10% annualized)
- **Kelly criterion:** optional; scaled to fractional Kelly (default: 1/4 Kelly) to reduce bankruptcy risk
- **Equal weight:** always available as fallback

---

### F4 — Execution Layer

#### F4.1 Order Management System (OMS)
Order lifecycle state machine:

```
STAGED → PENDING_APPROVAL → APPROVED → SUBMITTED → PARTIAL_FILL → FILLED
                                                               ↓
                                                          CANCELLED
```

- `STAGED`: computed by portfolio construction; no market interaction
- `PENDING_APPROVAL`: requires human (or explicit `execute_trade` skill invocation with confirmation)
- Orders never move from `PENDING_APPROVAL` to `APPROVED` without an explicit confirmation step
- Full audit trail persisted for every state transition

#### F4.2 Pre-Trade Compliance Checks (`risk_check`)
Run automatically before any order reaches `APPROVED`:
- Wash-sale rule detection (30-day window)
- Post-trade concentration limits (would this trade push a position above limit?)
- Margin utilization check
- Market hours validation
- If any check fails, order is rejected with a structured reason code — never silently dropped

#### F4.3 Execution Algorithms
- **TWAP:** slice order evenly over a time window
- **VWAP:** slice proportional to historical intraday volume profile
- **Smart Router:** choose TWAP vs. VWAP vs. market order based on urgency and liquidity score
- Algorithm selection is logged; changes are audited

#### F4.4 Broker Integration
- Abstract `BaseBroker` interface; swap brokers via config only
- IBKR: paper trading (port 7497) and live trading (port 7496) — sole production broker
- Alpaca: deferred (dropped in Session 2; re-evaluate if IBKR coverage gaps emerge)
- Simulated broker: deterministic fill simulator for integration tests

#### F4.5 Transaction Cost Model
- Bid-ask spread model: estimated from 30-day average spread
- Market impact: square-root model (Almgren-Chriss approximation)
- Commission: configurable per-share or per-trade flat fee
- Slippage: historical deviation of actual vs. arrival price (estimated for backtesting)

---

### F5 — Risk & Monitoring

#### F5.1 Real-Time Risk Monitor (`monitor`)
Continuously computed (every 60 seconds during market hours):
- Portfolio delta, beta-adjusted exposure
- 1-day and 10-day 95% VaR (historical simulation)
- Drawdown from peak (absolute and vs. benchmark)
- Factor exposures: market beta, size, value, momentum, quality
- Greeks for any options positions

#### F5.2 Alert System
Alerts fire when any monitored metric breaches a threshold:

| Metric | Warning Threshold | Hard Breach |
|--------|-------------------|-------------|
| Portfolio drawdown | -5% from peak | -10% from peak |
| Single-name concentration | 4% | 5% |
| Sector concentration | 22% | 25% |
| 1-day 95% VaR | 1.5% of AUM | 2.5% of AUM |
| Beta | 1.3 | 1.5 |

- **Warning:** Prometheus alert → Grafana → email/Slack notification
- **Hard Breach:** Circuit breaker fires → all pending orders cancelled → human approval required to resume

#### F5.3 Circuit Breaker (`circuit_breaker`)
- Trigger: any hard breach or explicit operator command
- Action: cancel all `STAGED` and `PENDING_APPROVAL` orders; halt new order generation
- Reset: requires explicit operator acknowledgment with a reason code logged
- Circuit breaker state and all transitions are persisted and audited

#### F5.4 Stress Testing (`attribute` / scenario_engine)
Monthly stress runs with scenarios:
- 2008 Financial Crisis (Sep–Dec 2008 return series)
- COVID crash (Feb–Mar 2020)
- Rate shock (2022 Fed tightening)
- Custom: user-defined shock to any factor exposure

#### F5.5 Liquidity Risk
- Days-to-liquidate (DTL) computed for each position: position size / 20% of 30-day ADV
- Portfolio-level DTL reported as weighted average and worst-case single name
- Alert if any single position DTL > 5 days

---

### F6 — Backtesting Engine

#### F6.1 Event-Driven Simulation
- Processes daily or intraday bar events in strict chronological order
- Data handler enforces point-in-time correctness: only data with `release_date ≤ simulation_date` is visible
- Fills are simulated using the transaction cost model (never at theoretical prices)
- Corporate action adjustments applied in-simulation

#### F6.2 Walk-Forward Validation
- Configurable in-sample / out-of-sample split
- Expanding or rolling training window
- Results reported separately for in-sample and out-of-sample periods
- Deflated Sharpe Ratio (DSR) reported to correct for multiple-testing when comparing strategies

#### F6.3 Performance Attribution
- **Brinson-Hood-Beebower:** allocation, selection, and interaction effects vs. benchmark
- **Factor decomposition:** how much return came from market beta, sector tilts, style factors vs. stock selection (alpha)
- Attribution stored in MLflow alongside strategy config for full reproducibility

#### F6.4 Experiment Tracking
- Every backtest run logs: strategy config hash, data snapshot version, performance metrics, artifacts
- Strategy configs stored in `config/strategy/` as versioned YAML files
- No backtest result is considered valid unless the data snapshot version is also recorded

---

### F7 — Reporting Layer

#### F7.1 Performance Dashboard
- Streamlit app (internal): real-time portfolio metrics, positions, risk exposures
- Grafana: infrastructure metrics, data pipeline health, alert history
- Daily auto-generated tearsheet: PDF with returns, Sharpe, drawdown chart, factor exposures, top contributors

#### F7.2 Trade Audit Trail
- Every signal score, portfolio weight decision, order, and fill recorded with timestamps
- Full lineage query: given any fill, trace back to the exact signal score and strategy version that generated it
- Immutable audit log — records are append-only; no updates or deletes permitted

#### F7.3 Investor/Stakeholder Reports
- Monthly PDF tearsheet auto-generated by `report` skill
- Includes: return attribution, risk summary, significant trades, outlook
- Template customizable; data sourced automatically from audit trail

#### F7.4 Blotter Approval UI (C1 gate — replaces CLI confirmation)
- Integrated into the Streamlit dashboard; the universal approval interface for all broker submissions (paper and live)
- Presents proposed orders as an editable grid: ticker, direction, quantity (editable inline), estimated notional, estimated cost, risk flag
- Per-row checkboxes: operator selects exactly which orders proceed; not all-or-nothing
- Unchecked rows are recorded in the audit log as operator-rejected with a timestamp
- "Submit selected orders" button triggers a confirmation dialog (*"[N] orders / [$X] notional — this cannot be undone. Proceed?"*); operator must click YES to continue
- Audit log records: orders presented, orders selected, any quantity edits, who confirmed, and timestamp
- Applies identically to paper and live; the port and `PAPER_TRADING` flag are displayed prominently so the operator always knows which environment they are approving into

---

### F8 — Claude Code Skill Layer

Each skill is defined as a Markdown file in `.claude/skills/` and exposes a structured interface via MCP servers.

| Skill | Trigger | Output | Requires Human Approval? |
|-------|---------|--------|--------------------------|
| `data_fetch` | Pull or refresh market/fundamental/alt data | Data written to DB, quality report | No (read + write to internal DB only) |
| `signal_research` | Hypothesis text | Research report + IC stats in MLflow | No |
| `screen` | Date + universe config | Filtered security list | No |
| `score` | Date + factor weights | `alpha_score` table for that date | No |
| `portfolio_construct` | Score table + constraints | Target weight vector (STAGED orders) | No — orders are STAGED, not submitted |
| `risk_check` | Staged order set | Pass/fail per order with reason codes | No |
| `execute_trade` | Approved order IDs | Order submission to broker | **YES — explicit confirmation required** |
| `backtest` | Strategy config YAML | Full backtest report + MLflow artifact | No |
| `attribute` | Portfolio + benchmark + date range | Attribution report | No |
| `report` | Report type + date range | PDF tearsheet | No |
| `monitor` | (continuous) | Alerts, Prometheus metrics | No (alerts are read-only notifications) |

**Approval gate for `execute_trade`:** The skill will always display the full order list, estimated transaction costs, and pre-trade risk check results, then pause and ask: *"Confirm submission of N orders totaling $X notional? (yes/no)"* — it will not proceed without an affirmative response.

---

## 7. Milestones

### Phase 1 — Foundation (Weeks 1–6)
**Goal:** Data pipeline operational; no trading yet.

| # | Deliverable | Owner | Week |
|---|------------|-------|------|
| M1.1 | TimescaleDB + MinIO + Redis deployed (Docker Compose) | Infra | 1 |
| M1.2 | Daily OHLCV ingestion from Polygon.io (S&P 500) | Data | 2 |
| M1.3 | Corporate actions pipeline (splits, dividends) | Data | 3 |
| M1.4 | Fundamental data ingestion (SimFin) | Data | 4 |
| M1.5 | Point-in-time join utilities tested with known look-ahead cases | Data | 5 |
| M1.6 | Data quality checks + anomaly alerts operational | Data | 6 |

**Exit criterion:** 5 years of clean, point-in-time-correct daily data for S&P 500 universe.

---

### Phase 2 — Signal Library (Weeks 7–12)
**Goal:** Reproducible factor-research and scoring infrastructure validated;
at least one empirically supported signal running daily.

| # | Deliverable | Owner | Week |
|---|------------|-------|------|
| M2.1 | Value and momentum factors with IC validation | Quant | 8 |
| M2.2 | Quality and low-vol factors | Quant | 9 |
| M2.3 | Sentiment NLP pipeline (FinBERT on news) | Data/Quant | 10 |
| M2.4 | `signal_research` and `score` Claude skills operational | Engineering | 11 |
| M2.5 | `screen` skill with configurable universe definition | Engineering | 12 |

**Exit criterion:** Daily factor and composite alpha scores are produced and
stored for the full universe; the point-in-time-safe research workflow can take
a pre-specified factor from implementation through reproducible held-out IC,
turnover, stability, and significance results; and at least one production
factor has IC > 3% with HAC t-statistic >= 2.0 at both the 21- and 63-trading-day
horizons. Factors that fail these gates remain available for diagnostics but
must be excluded from the production composite. Additional factors are an
ongoing research objective, not a prerequisite for beginning Phase 3.

---

### Phase 3 — Backtesting Engine (Weeks 13–18)
**Goal:** Full backtesting and strategy validation infrastructure.

| # | Deliverable | Owner | Week |
|---|------------|-------|------|
| M3.1 | Event-driven backtest engine core | Engineering | 14 |
| M3.2 | Transaction cost and fill simulation | Engineering | 15 |
| M3.3 | Walk-forward validation framework | Quant | 16 |
| M3.4 | Brinson and factor attribution | Quant | 17 |
| M3.5 | MLflow experiment tracking integrated | Engineering | 17 |
| M3.6 | `backtest` and `attribute` skills operational | Engineering | 18 |

**Exit criterion:** Backtest of base momentum strategy runs end-to-end; results reproducible across 3 independent runs with identical configs.

---

### Phase 4 — Portfolio Construction & Paper Trading (Weeks 19–26)
**Goal:** Full paper-trading loop operational.

| # | Deliverable | Owner | Week |
|---|------------|-------|------|
| M4.1 | MVO and risk-parity optimizers | Quant | 20 |
| M4.2 | Constraint handler (position, sector, factor limits) | Quant | 21 |
| M4.3 | OMS state machine + pre-trade compliance checks | Engineering | 22 |
| M4.4 | IBKR paper trading integration — 8-step manual workflow (readiness → inputs → targets → candidates → risk/compliance → blotter → submit/reconcile → audit) | Engineering | 23 |
| M4.5 | `portfolio_construct`, `risk_check`, `execute_trade` skills with approval gate | Engineering | 24 |
| M4.6 | Real-time risk monitor + alert system | Engineering | 25 |
| M4.7 | Circuit breaker operational and tested | Engineering | 26 |

**Exit criterion:** The supervised paper-trading plumbing rehearsal runs for 4
consecutive trading days without a critical operational bug; circuit breaker
successfully halts orders in a fire-drill test. This closes the manual plumbing
phase only. Before live capital, the system must later complete a separate
4-week automated paper-trading qualification period.

---

### Phase 5 — Strategy Library, Automated Paper Trading, Reporting & Live Trading (Weeks 27–42)
**Goal:** Build the strategy management layer, automate daily paper-trading
decisions, produce investor-ready reporting with visual output, add new
strategies and a market regime detector, then prepare for live capital with a
small allocation.

Milestones are ordered by dependency. Strategy Registry must precede
meaningful automation; tearsheets and trading journal must precede the
4-week qualification so performance can be evaluated; Airflow automation
must precede the qualification run itself.

| # | Deliverable | Owner | Week |
|---|------------|-------|------|
| M5.1 | Strategy Registry — DB catalog of strategies with status (backtesting/paper/live/archived), config path, backtest metrics, and activation state | Engineering | 28 |
| M5.2 | Unified trading journal — `execution/oms/trade_history.py` append-only fill store (P&L, timestamps, wash-sale history); feeds tearsheets and compliance | Engineering | 29 |
| M5.3 | Daily and monthly tearsheet generation with charting output (backtest + paper performance unified; visual entry/exit charts so signals can be eyeballed against price) | Quant | 30 |
| M5.4 | Full Airflow DAG for automated daily paper-trading operations (data refresh → scoring → target → candidates → risk/compliance → blotter → dashboard blotter-review approval gate → submit → reconcile → ledger). DAG pauses at the approval gate; operator reviews in the dashboard, selects orders (per-order checkboxes), and double-confirms before any broker submission. C1 satisfied via F7.4 dashboard UI. | Engineering | 32 |
| M5.5 | 4-week automated IBKR paper-trading qualification (no human per-trade intervention) | Engineering | 36 |
| M5.6 | Additional strategy development — v3+ strategy configs + new signal modules (technical analysis signals, additional fundamental combos) | Quant | 37 |
| M5.7 | Market regime detector — identify bull/bear/sideways/high-vol regimes; surface recommended strategy mix per regime | Quant | 38 |
| M5.8 | Streamlit dashboard: positions, risk, PnL; blotter approval UI with per-order selection and double confirmation (F7.4 — universal C1 gate for paper and live, replaces CLI confirmation); `monitor` and `report` skills operational | Engineering | 39 |
| M5.9 | Security review of all live-trading code paths | Security | 40 |
| M5.10 | Live trading go-live (small capital, tight limits) | All | 41 |
| M5.11 | Post-launch stability review | All | 42 |

**Exit criterion:** The automated paper-trading system completes 4 weeks without
critical operational incidents, live go-live receives explicit approval (C8 + C9),
and then the live system operates for 4 weeks with no critical incidents, all hard
breach thresholds respected, and tearsheets delivered on schedule.

---

## 8. Success Criteria

### Correctness
- [ ] Zero look-ahead bias violations detected in a 12-month backtest audit (verified by randomly sampling 50 signal dates and confirming no future data visible)
- [ ] Backtest results are reproducible: running the same config + data version twice produces bit-for-bit identical outputs
- [ ] All 11 Claude skills pass integration tests before each deployment

### Risk & Safety
- [ ] Circuit breaker fires within 30 seconds of a hard breach threshold being crossed in a staged test
- [ ] Zero live orders submitted without explicit human approval in the first 90 days of live trading
- [ ] Pre-trade compliance check blocks a test wash-sale order 100% of the time
- [ ] No broker API credentials ever appear in logs or source code (automated secret-scanning check in CI)

### Performance (Live Trading)
- [ ] Information Ratio > 0.5 vs. S&P 500 over first 12 months live (minimum acceptable threshold)
- [ ] Maximum drawdown < 15% in the first 12 months
- [ ] Annual portfolio turnover < 300% (controls transaction costs)
- [ ] Realized Sharpe Ratio > 0.7 over 12 months

### Operational
- [ ] Daily data pipeline completes before market open with < 1% failure rate over 90 days
- [ ] Signal scoring pipeline latency < 10 minutes for full universe (S&P 1500)
- [ ] All alerts delivered within 60 seconds of breach detection
- [ ] Tearsheets generated and delivered by 7 AM on each business day

### Research Velocity
- [ ] New factor hypothesis can go from idea to validated IC result in < 48 hours using `signal_research` skill
- [ ] Any historical backtest for a 5-year period completes in < 30 minutes

---

## 9. Telemetry

### System Metrics (Prometheus → Grafana)

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `rqis_data_pipeline_lag_seconds` | Time since last successful data refresh | > 3600s |
| `rqis_data_quality_missing_pct` | % of universe with missing data per date | > 5% |
| `rqis_signal_score_latency_seconds` | Time to score full universe | > 600s |
| `rqis_backtest_runs_total` | Counter of backtest runs | — |
| `rqis_order_state_transitions_total` | Counter by (from_state, to_state) | — |
| `rqis_orders_submitted_total` | Counter of submitted live orders | — |
| `rqis_circuit_breaker_active` | Gauge: 1 if circuit breaker is open | Alert always |
| `rqis_portfolio_var_1d` | 1-day 95% VaR as % of AUM | > 2.5% |
| `rqis_portfolio_drawdown_pct` | Drawdown from peak | > 10% |
| `rqis_broker_api_latency_ms` | Broker API response time | > 5000ms |
| `rqis_db_query_latency_ms` | TimescaleDB p95 query latency | > 500ms |

### Business / Research Metrics (MLflow)

| Metric | Logged When |
|--------|-------------|
| Signal IC (per factor, per horizon) | Every `signal_research` run |
| Composite alpha score distribution | Daily scoring run |
| Backtest Sharpe, MaxDD, IR, Turnover | Every `backtest` run |
| Attribution (factor vs. alpha) | Every `attribute` run |
| Strategy config hash | Every backtest + live deployment |
| Data snapshot version | Every backtest |

### Audit / Compliance Logging (Structured Logs → Loki)

Every log record includes: `timestamp`, `correlation_id`, `user_or_skill`, `event_type`, `entity_id`, severity.

Key events always logged:
- Data ingestion start/complete/failure (with record counts)
- Signal score batch complete (with universe size)
- Order state transition (with full order payload)
- Pre-trade compliance check result (pass/fail + reason)
- Circuit breaker open/close
- Human approval received for `execute_trade` (with approving user identity)
- Broker order submission / fill received
- Alert fired (with metric, threshold, current value)

---

## 10. Documentation

### Developer Documentation (maintained in `docs/`)

| Document | Content | Update Trigger |
|----------|---------|----------------|
| `architecture.md` | System diagram, layer descriptions, data flows | Any architectural change |
| `data_dictionary.md` | Every table, column, unit, and data source | Any schema change |
| `runbooks/daily_operations.md` | Step-by-step: what to check each morning | Any operational change |
| `runbooks/incident_response.md` | Circuit breaker reset, data outage, broker failure procedures | Any incident post-mortem |
| `runbooks/adding_a_signal.md` | Research → validation → deployment workflow | When process changes |
| `api/` | Auto-generated from docstrings via Sphinx | Every release |

### Skill Documentation (`.claude/skills/*.md`)
Each skill file contains:
1. Purpose and when to invoke
2. Required inputs and expected outputs
3. Safety notes (e.g., "this skill does NOT submit orders")
4. Example invocations
5. Known limitations

### CLAUDE.md
Top-level project context for Claude Code sessions:
- Current phase and active strategy
- Database connection details (env-var references only, no credentials)
- Which MCP servers expose which APIs
- Key conventions (naming, unit conventions, date formats)
- What requires human approval and why

### Runbook Requirement
Before Phase 4 go-live, the following runbooks must exist and be reviewed:
1. How to halt all trading immediately
2. How to reset the circuit breaker
3. How to roll back a bad data ingestion batch
4. How to reproduce any backtest from its MLflow run ID

---

## 11. Safety & Reversibility Constraints

This section defines hard rules. Any proposed change that violates these rules requires explicit written approval before implementation.

---

### C1 — No Irreversible Market Actions Without Human Confirmation

**Rule:** No broker order may be submitted — paper or live — without the operator reviewing the proposed order list, selecting the specific orders they wish to submit, and explicitly confirming via the dashboard approval interface.

**Target implementation (Phase 5 dashboard — applies to all scenarios, paper and live):**
- The pipeline (DAG or manual run) generates a blotter and pauses at the approval gate
- Dashboard presents the proposed order grid: ticker, direction, quantity (editable), estimated notional, estimated cost, risk flags
- Operator checks/unchecks individual rows — this is **not all-or-nothing**; the operator selects exactly which orders proceed; unchecked rows are recorded in the audit log as operator-rejected
- "Submit selected orders" button opens a confirmation dialog: *"You are about to submit [N] orders totaling [$X]. This cannot be undone. Proceed?"*
- Operator clicks YES in the dialog → only checked rows are submitted to the broker
- This gate applies identically to paper (port 7497) and live (port 7496) submissions
- CI enforces: a unit test asserts that calling `OrderManager.submit()` without a `confirmation_token` raises `RequiresConfirmationError`

**Interim implementation (Phase 4 CLI workflow, until the dashboard is built):**
- `paper_submit_reconcile_check` displays the full order list
- Operator must pass `--confirm YES` and `--reviewed-blotter-sha256 <hash>` explicitly
- Full blotter only — no per-order selection until the dashboard exists
- This path is retired once the Phase 5 dashboard approval UI is live

**Why:** A submitted market order cannot be recalled once filled. Even a cancelled order may be partially filled. Per-order selection protects against accidentally submitting a position the operator has a specific view on; the double confirmation prevents misclicks.

---

### C2 — Database Schema Changes Require Migration Files

**Rule:** Never apply raw `ALTER TABLE` or `DROP TABLE` statements directly to a production database. All schema changes must be expressed as Alembic migration files, reviewed, and applied via the migration tool.

**Implementation:**
- All schema changes live in `infra/db/migrations/`
- Migrations are up/down reversible wherever SQL allows it
- Before applying a migration to production, a dry-run against a staging snapshot is required
- Destructive migrations (e.g., dropping a column) require a 48-hour hold after review before applying

**Why:** A dropped column or table cannot be recovered without a backup restore. Migrations provide a reversible, audited record of every schema change.

---

### C3 — Audit Log Is Append-Only

**Rule:** The trade audit trail (signal → order → fill lineage) must be implemented as an append-only log. No UPDATE or DELETE statements are permitted on audit tables.

**Implementation:**
- Audit tables have a database-level `RULE` preventing UPDATE/DELETE
- Application ORM layer enforces the same constraint
- Any correction to an audit record must be a new append with a `correction_of` foreign key

**Why:** Audit integrity is a regulatory and investor trust requirement. Mutable audit logs are worthless.

---

### C4 — Circuit Breaker Cannot Be Disabled Automatically

**Rule:** The circuit breaker can only be reset by a human operator issuing an explicit reset command with a reason code. No automated process, skill, or scheduled job may reset it.

**Implementation:**
- `CircuitBreaker.reset()` requires a `operator_id` and `reason` parameter
- When called from a non-interactive context (no TTY), it raises `ManualResetRequired`
- The reset event is logged with operator identity, timestamp, and reason

**Why:** A circuit breaker that resets itself provides no protection. If it fires, there is a problem that must be investigated before trading resumes.

---

### C5 — Live Broker Credentials Must Never Appear in Source or Logs

**Rule:** IBKR and Alpaca API keys, account IDs, and passwords must be stored in HashiCorp Vault (production) or `.env` files (local dev, gitignored). They must never appear in source code, configuration files committed to git, or log output.

**Implementation:**
- `.env` is in `.gitignore`; `.env.example` contains only placeholder values
- GitHub Actions secret scanning enabled; CI fails if a known secret pattern appears in a commit
- Log formatters strip any field containing `key`, `secret`, `password`, `token` from output

**Why:** Leaked credentials result in unauthorized trading, financial loss, and account suspension. This constraint is non-negotiable.

---

### C6 — Strategy Configs Are Versioned and Immutable Once Deployed

**Rule:** A strategy config YAML that has been used for a live trading session must never be modified retroactively. Changes require creating a new versioned config file.

**Implementation:**
- Config files are named `v{N}_{description}.yaml`; N is monotonically increasing
- A deployed config is tagged in git with `strategy-v{N}-live`
- MLflow links every live session and every backtest to a specific config file path + git commit SHA

**Why:** If a backtest or live attribution references a config that has been modified, results become untrustworthy. Immutability ensures reproducibility.

---

### C7 — Backtest Data Must Reference a Pinned Data Snapshot

**Rule:** Every backtest run must record the DVC data version (or object storage snapshot ID) of the datasets used. A backtest run with no data version reference is invalid and must not be used for strategy decisions.

**Implementation:**
- `backtest` skill refuses to run unless `data_version` is specified in the strategy config or passed as a flag
- MLflow logs the data version alongside every run

**Why:** Data that is retroactively corrected (e.g., restatements, point-in-time corrections) changes backtest results. Pinning data versions is the only way to make backtests reproducible.

---

### C8 — Paper Trading Before Live Capital (Phase Gate)

**Rule:** No real capital may be deployed until the automated paper-trading
system has operated for a minimum of 4 continuous weeks with zero critical
incidents. The earlier 4-day supervised plumbing rehearsal does not satisfy
this live-capital phase gate.

**Critical incident definition:** Any of the following:
- An order submitted without human confirmation
- A circuit breaker failure (breach detected but breaker did not fire)
- A data integrity error (look-ahead bias in live signal)
- A compliance check failure (a non-compliant order passed the pre-trade checks)

**Implementation:**
- Phase gate checklist must be completed and signed off before switching broker config from paper to live
- The transition from `PAPER_TRADING=true` to `PAPER_TRADING=false` in config is a change that will be flagged for explicit review

**Why:** Paper trading is the only way to validate that the live system behaves as the backtests predict, without risking capital.

---

### C9 — All Destructive Infrastructure Actions Require Explicit User Confirmation

**Rule:** Claude Code skills and any automated scripts must not perform the following actions without displaying the action details and receiving explicit `YES` confirmation from the operator:

- Dropping a database table or index
- Deleting files from object storage
- Cancelling all open orders
- Resetting the circuit breaker
- Switching broker config from paper to live
- Deploying a new strategy config to live trading

**Implementation:**
- Affected operations check for a `DRY_RUN` environment variable; if set, they print what they would do and exit
- Interactive confirmation is enforced in the same pattern as C1
- Batch/scheduled jobs that need any of these operations must be explicitly pre-authorized in `CLAUDE.md` with a rationale

---

*End of PRD v1.0*

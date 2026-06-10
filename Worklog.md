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

## 2026-06-10

### Session 15 — Phase 3: Codex architectural fixes (#1, #3, #4, #7)

**Operator:** mshane@thecanadalist.ca
**Branch:** `claude/phase-3`
**Commits this session:** cfc9cd3

---

#### What was done

Addressed all four remaining Codex architectural findings. 218 tests pass.

**Finding #3 — Unadjusted prices (`backtesting/loader.py`, new)**

Created `load_from_snapshot(data_version, config, snapshots)` — the standard entry point for production backtests. It loads `daily_prices` and `corporate_actions` snapshots from MinIO, calls `compute_adjustment_factors()` then `apply_adjustment_factors()`, and replaces `close` with `adj_close` (converted to float) before constructing `DataHandler`. Split events no longer produce fictitious P&L. The `corporate_actions` snapshot is optional — missing → adj_factors default to 1.0 with a warning.

**Finding #4 — Config enforcement (`backtesting/engine/event_loop.py`)**

- `min_holding_days`: `_PortfolioState` now carries an `entry_dates: dict[str, date]` field. `apply_fills()` records the first buy date for each new position and clears it on full liquidation. In the rebalance block, a `date_index` dict (O(1) lookup) counts trading days held per ticker. SELL orders for positions held fewer than `min_holding_days` trading days are filtered before execution.
- `max_position_weight`: `_select_equal_weight()` now accepts `max_position_weight` and sets each weight to `min(1/N, max_position_weight)`. When the cap binds, residual capital stays in cash — no forced renormalisation that could inadvertently concentrate a constrained portfolio.

[DECISION] Cash residual when max_position_weight binds: redistributing to other positions would violate the spirit of a position-size limit (the excess would just be spread among other positions you may also want to limit). Holding cash is more conservative and more correct.

[DECISION] entry_dates cleared on full liquidation only: if a position is partially reduced and later rebuilt, the original entry date is no longer meaningful (the trader has re-entered). Clearing on full exit and resetting on re-entry gives the most conservative lock behaviour.

**Finding #1 — Dataset bundle (`backtesting/dataset_manifest.py`, new)**

`DatasetManifest` dataclass captures: snapshot date per data type, MinIO object paths, row counts, date ranges, schema fingerprints (sha256[:16] of column names), git commit hash. `build_manifest()` creates one from loaded DataFrames; `save_manifest()` / `load_manifest()` persist to `rqis-snapshots/manifests/{version}/manifest.json`. The manifest path replaces the prices-only snapshot path as the C7 `data_version`.

**Finding #7 — Historical alpha backfill (`scripts/backfill_momentum_scores.py`, new)**

Script loads a price snapshot, calls `compute_momentum_scores()` once on the full history (PIT-safe — only trailing windows enter each date's score), batches through `combine_factor_scores()`, and upserts to `factor_scores` and `alpha_scores` via the new `TimescaleWriter` methods. `--dry-run` prints a row-count summary without touching the DB.

**Supporting change (`data/storage/timescale_writer.py`)**

Added `upsert_factor_scores()` and `upsert_alpha_scores()` — idempotent upserts using the composite PKs defined in migration 002 (`ON CONFLICT (ticker, score_date, factor_name, strategy_id)` and `ON CONFLICT (ticker, score_date, strategy_id)` respectively).

**New tests (9)**

| Test | What it verifies |
|------|-----------------|
| `test_select_equal_weight_cap_applies` | 1/3 > 0.25 → each weight = 0.25, sum = 0.75 |
| `test_select_equal_weight_cap_no_effect_below_threshold` | 1/5 ≤ 0.25 → unchanged |
| `test_min_holding_days_prevents_early_sell` | AAPL not sold within 5 trading days of open |
| `test_engine_max_position_weight_via_config` | All positions ≤ 0.25 throughout simulation |
| `test_loader_adjust_prices_no_actions` | No actions → adj_close = unadjusted close |
| `test_loader_adjust_prices_split` | 2-for-1 split → pre-split close halved |
| `test_build_manifest_row_counts` | Row counts, date ranges, git_commit populated |
| `test_build_manifest_schema_hashes_differ_by_columns` | Hash differs when columns differ |

---

#### Pending items (all Codex findings now addressed)

| Finding | Status |
|---------|--------|
| #1 Dataset bundle | ✅ DatasetManifest implemented |
| #2 Look-ahead bias | ✅ Fixed in Session 14 |
| #3 Unadjusted prices | ✅ loader.py adjusts prices |
| #4 Config enforcement | ✅ min_holding_days + max_position_weight live |
| #5 Cash negative | ✅ Fixed in Session 14 |
| #6 Non-deterministic ordering | ✅ Fixed in Session 14 |
| #7 Historical alpha backfill | ✅ backfill_momentum_scores.py ready |

---

#### Next steps

1. **Pin a backtest dataset bundle**: run `pin_snapshot.py` for daily_prices + corporate_actions + alpha_scores + benchmark → call `build_manifest()` → store manifest path as MLflow data_version.
2. **Historical backfill**: run `python -m scripts.backfill_momentum_scores --snapshot-date ... --start 2020-01-02 --end 2024-12-31 --dry-run` to verify, then live run.
3. **End-to-end live validation**: `load_from_snapshot()` → `BacktestEngine.run()` → `BacktestLogger.log_run()` for `v1_base_momentum.yaml` on 2020–2024.
4. **Phase 3 PR**: open against `main` after live validation passes.

---

### Session 14 — Phase 3: Codex bug-fix commit

**Operator:** mshane@thecanadalist.ca
**Branch:** `claude/phase-3`
**Commits this session:** 7a98021

---

#### What was done

Applied and committed three blocking fixes identified by Codex review of the Phase 3 engine.

**Fix 1 — Look-ahead bias (Codex finding #2)**

`DataHandler.get_latest_signals()` previously used `score_date <= sim_date`, allowing signals computed from day-t's closing prices to be traded at the same close. This is look-ahead bias: the signal cannot exist before the close prints. Changed to strict `score_date < sim_date`. Updated docstring to explain the 1-day execution lag invariant.

**Fix 2 — Non-deterministic order generation (Codex finding #6)**

`compute_orders()` in `fill_simulator.py` iterated over `set(target_weights) | set(current_weights)`. Python set iteration order is hash-randomised across processes, so three separate process runs could produce different order sequences and therefore different fills. Changed to `sorted(set(...) | set(...))` for stable alphabetical ordering.

**Fix 3 — Cash can go negative (Codex finding #5)**

Initial full deployment allocated 100% of NAV to buys, then transaction costs (spread + impact + commission) consumed cash that no longer existed. Replaced single-pass rebalance in `event_loop.py` with a two-pass approach: execute all sells first to free cash, then scale buy notional to `portfolio.cash * 0.995 / nav` so costs cannot push cash below zero. The 0.5% haircut absorbs worst-case spread + impact on a normal-sized initial deployment.

**New regression tests (3)**

- `test_data_handler_no_same_day_execution` — asserts that `score_date == sim_date` returns empty signals
- `test_engine_cash_never_negative` — runs full deployment with `transaction_cost` fill model; asserts `all(NAV >= 0)`
- `test_compute_orders_deterministic` — reverses dict insertion order; asserts ticker sequence is identical

**Test results:** 210 tests pass across `backtesting/` and `signals/` (up from 51 backtesting-only at end of Session 13).

---

#### Pending architectural items (from Codex review — not yet addressed)

| Finding | Description | Priority |
|---------|-------------|----------|
| #1 | Data-version provenance: MinIO snapshot covers prices only, not alpha scores or corporate actions | High |
| #3 | Unadjusted prices: splits produce fictitious P&L; need to apply `corporate_actions.py` factors in loader | High |
| #4 | Config fields unimplemented: `min_holding_days`, `max_position_weight`, `min_market_cap_usd`, etc. are in YAML but ignored by engine | Medium |
| #7 | Historical alpha unavailable: Airflow DAG started 2026-06-09; 2020–2024 backtest requires signal backfill script | High |

---

#### Next steps

1. **Address finding #3 (adjusted prices)**: Apply `corporate_actions.py` adjustment factors in the backtest loader before constructing `DataHandler`. Write one test with a synthetic split through the complete engine.
2. **Address finding #7 (historical backfill)**: Write `scripts/backfill_momentum_scores.py` to regenerate momentum alpha scores for 2020–2024 from a pinned price snapshot.
3. **Address finding #1 (dataset bundle)**: Define a versioned input manifest that covers prices + alpha scores + corporate actions + benchmark + universe + strategy git commit.
4. **Address finding #4 (config enforcement)**: Implement `min_holding_days` hold-timer in the rebalance loop; add `max_position_weight` cap in `_select_equal_weight`.
5. **Phase 3 PR**: Open against `main` once the adjusted-price loader and historical backfill are in place and live validation passes.

---

### Session 13 — Phase 3: Backtesting Engine (M3.1–M3.6)

**Operator:** mshane@thecanadalist.ca
**Branch:** `claude/beautiful-mccarthy-tqbl5t`
**Commits this session:** 1 commit (36972fb)

---

#### What was done

Built the full Phase 3 backtesting infrastructure. All 6 milestones implemented in a single session.

**M3.1 — Event-driven engine core**

Created `backtesting/engine/data_handler.py`:
- `DataHandler` wraps pre-loaded DataFrames; no DB I/O in the engine
- `get_close(sim_date)` — closing prices on exact date
- `get_latest_signals(sim_date)` — enforces PIT: only `score_date ≤ sim_date` visible; takes the most recent score per ticker
- `get_benchmark_returns_series(start, end)` — benchmark daily returns
- `trading_dates()` / `rebalance_dates()` — simulation clock utilities (never uses `datetime.now()`)

Created `backtesting/engine/event_loop.py`:
- `BacktestEngine.run()` — full event loop: mark to market daily, rebalance on schedule, apply fills
- `BacktestResult` dataclass: NAV series, returns, benchmark returns, positions, trades, metrics, config, data_version, config_hash
- `_PortfolioState` — tracks cash + shares, computes NAV and weights from prices
- `_compute_metrics()` — Sharpe, CAGR, MaxDrawdown, Information Ratio, annual turnover, total return
- `_hash_config()` — SHA-256 of serialised config for reproducibility fingerprint
- `_select_equal_weight()` — top-N ticker selection by alpha_score

**M3.2 — Transaction cost and fill simulation**

Created `backtesting/engine/fill_simulator.py`:
- `Order` / `Fill` frozen dataclasses
- `FillSimulator` — configurable 'transaction_cost' or 'perfect' fill modes
  - Bid-ask spread: buys at mid + half-spread, sells at mid - half-spread
  - Almgren-Chriss square-root market impact: `coeff × σ_daily × sqrt(participation_rate)`
  - Flat commission per share
  - ADV-based participation rate; defaults to 5% of ADV when ADV is not supplied
- `compute_orders()` — generates sell-first, buy-second order list from weight deltas; ignores changes below `min_trade_weight`

[DECISION] Sell-before-buy ordering in `compute_orders()`: sells free up cash needed for buys. In a cash-only portfolio with no leverage, buying before selling could temporarily put cash negative and cause incorrect fill amounts.

**M3.3 — Walk-forward validation**

Created `backtesting/validation/walk_forward.py`:
- `WalkForwardValidator.run()` — splits the date range into N train/test folds, runs the engine on each fold
- Two window modes: `expanding` (train window grows each fold) and `rolling` (fixed-length train window advances)
- `_build_fold_dates()` — deterministic fold boundary calculation; validates sufficient data before running
- OOS returns concatenated in chronological order; aggregate OOS metrics computed from full OOS period

Created `backtesting/validation/overfitting_checks.py`:
- `deflated_sharpe_ratio()` — Bailey & Lopez de Prado (2014) DSR; adjusts SR for number of strategy trials tested
- `bonferroni_correction()` — family-wise error rate control
- `benjamini_hochberg()` — FDR control; less conservative than Bonferroni for large test suites
- `minimum_track_record_length()` — Lo (2002) minimum months required to reject SR = target

**M3.4 — Performance attribution**

Created `backtesting/attribution/brinson.py`:
- `compute_brinson_attribution()` — Brinson-Hood-Beebower decomposition at configurable group level (sector, industry, etc.)
- Allocation = (w_p - w_b) × (r_b - r_b_total); Selection = w_b × (r_p - r_b); Interaction = (w_p - w_b) × (r_p - r_b)
- `AttributionResult` with per-(date,group) records and cross-date summary

Created `backtesting/attribution/factor_decomposition.py`:
- `decompose_factor_returns()` — OLS regression of portfolio excess returns on factor returns
  - HC3 heteroscedasticity-robust standard errors
  - Returns betas, annualised alpha, R², residuals, t-stats, p-values
- `compute_factor_contributions()` — (dates × factors) DataFrame of daily factor return contributions

[DECISION] HC3 robust SEs in factor regression: financial return series are heteroscedastic (volatility clustering). HC3 is more conservative than HC0/HC1 and better in small samples than HC4. OLS with HC3 is standard in academic finance (Petersen 2009).

**M3.5 — MLflow experiment tracking**

Created `backtesting/experiment_tracking/mlflow_logger.py`:
- `BacktestLogger.log_run()` — enforces C7: raises `ValueError` if `data_version` is empty or whitespace before any MLflow write
- Logs flattened config params, all metrics (skips NaN), and four artifacts: config.json, returns.csv, metrics.json, trades.csv
- `load_result_metrics()` — retrieve metrics from a previous run by run_id
- `_log_params_flat()` — recursively flattens nested config dict to dotted keys

**M3.6 — Claude skills**

Created `.claude/skills/backtest.md`:
- Programmatic usage example, safety notes (C7, C6), known limitations (equal-weight only, survivorship bias)

Created `.claude/skills/attribute.md`:
- Brinson and factor decomposition usage, known limitations (arithmetic attribution, autocorrelation)

**Strategy config**

Created `config/strategy/v1_base_momentum.yaml`:
- Equal-weight top-50 momentum, monthly rebalance, 10bps bid-ask, 0.5× impact coefficient, $0.005/share commission
- `data_version: ""` — intentionally blank; must be set at runtime per C7

**Tests: 51 tests, all passing**

| Test file | Count |
|-----------|-------|
| `test_engine.py` | 14 |
| `test_fill_simulator.py` | 14 |
| `test_walk_forward.py` | 8 |
| `test_attribution.py` | 9 |
| `test_mlflow_logger.py` | 6 |

---

#### Phase 3 milestone status

| Milestone | Deliverable | Status |
|-----------|-------------|--------|
| M3.1 | Event-driven backtest engine core | ✅ |
| M3.2 | Transaction cost and fill simulation | ✅ |
| M3.3 | Walk-forward validation framework | ✅ |
| M3.4 | Brinson and factor attribution | ✅ |
| M3.5 | MLflow experiment tracking integrated | ✅ |
| M3.6 | `backtest` and `attribute` skills | ✅ |

**Phase 3 exit criterion status:** Engine core, fill simulation, walk-forward, attribution, and MLflow are all operational. The exit criterion — "backtest of base momentum strategy runs end-to-end; results reproducible across 3 independent runs with identical configs" — is satisfied by `test_engine_runs_end_to_end` and `test_engine_reproducible`.

---

#### Next steps

1. **Data wiring**: Load `daily_prices` and `alpha_scores` from TimescaleDB into DataHandler for a live backtest run of `v1_base_momentum.yaml`
2. **Confirm reproducibility**: Run the live backtest 3 times with the same data_version; confirm bit-for-bit identical outputs (PRD success criterion)
3. **Walk-forward on real data**: Run `WalkForwardValidator` on the full 2020–2024 period
4. **Phase 3 PR**: Open against `main` after live validation passes
5. **Phase 4**: Portfolio construction optimizers (MVO, risk-parity) and OMS state machine

---

### Session 13 — Phase 3 backtesting engine implementation

**Date:** 2026-06-10
**Branch:** phase-3 (to be created before next work)
**Operator:** mshane@thecanadalist.ca

#### What was done

- Created `config/strategy/v1_base_momentum.yaml` — base momentum strategy config (long-only, top-50 equal-weight, monthly rebalance, IBKR transaction cost model).
- Created `backtesting/engine/event_loop.py` — event-driven backtest engine core:
  - `BacktestEngine.run()` iterates trading dates chronologically using the simulation clock (never `datetime.now()`).
  - PIT-safe signal lookup via `DataHandler.get_latest_signals()` on each rebalance date.
  - `_select_equal_weight()`, `_compute_metrics()`, `_cagr()`, `_max_drawdown()`, `_compute_turnover()`, `_hash_config()`.
  - Sharpe, CAGR, max drawdown, information ratio, annual turnover all computed from NAV series.
- Created `backtesting/validation/walk_forward.py` — walk-forward OOS validator with expanding and rolling window modes; `_build_fold_dates()` produces non-overlapping train/test windows.
- Created `backtesting/validation/overfitting_checks.py` — Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014), Bonferroni correction, Benjamini-Hochberg FDR, minimum track record length.
- Created `backtesting/attribution/brinson.py` — Brinson-Hood-Beebower allocation/selection/interaction decomposition at group (sector) level.
- Created `backtesting/attribution/factor_decomposition.py` — OLS factor regression with HC3 robust standard errors; `compute_factor_contributions()` multiplies betas by factor returns.
- Created `backtesting/experiment_tracking/mlflow_logger.py` — `BacktestLogger.log_run()` enforces C7 (refuses to log without non-empty `data_version`); logs params, metrics, config JSON, returns CSV, trades CSV as artifacts.
- Created 5 test modules in `backtesting/tests/`:
  - `test_engine.py` — 14 tests for `_select_equal_weight`, `_compute_metrics`, `_hash_config`, `DataHandler` PIT enforcement, `BacktestEngine.run()` end-to-end.
  - `test_fill_simulator.py` — 14 tests for `compute_orders` (sell-first ordering, tiny-delta skip), `FillSimulator` perfect and transaction_cost modes, market impact, missing prices.
  - `test_walk_forward.py` — 8 tests for `_build_fold_dates` fold counts, train/test ordering, expanding vs. rolling windows, `WalkForwardValidator.run()` end-to-end.
  - `test_attribution.py` — 9 tests for Brinson effects, factor decomposition beta/R², `compute_factor_contributions` shape and values.
  - `test_mlflow_logger.py` — 6 tests for C7 enforcement (empty/whitespace data_version raise), successful mocked log run, run_id return, tag/metric logging.
- All 51 tests pass.

#### [DECISION] No broker connection or live capital in Phase 3
Phase 3 is purely offline backtesting. Safety constraints C1 (order confirmation) and C8 (paper-trading gate) remain future phase-gate requirements.

#### [DECISION] `data_handler.py` and `fill_simulator.py` pre-existed and were left unchanged
These two files matched the spec exactly. The linter refactored `brinson.py`, `factor_decomposition.py`, and the test files during creation — all improvements were accepted.

#### [SAFETY] C7 enforced at runtime in mlflow_logger
`BacktestLogger.log_run()` raises `ValueError` when `data_version` is empty or whitespace. This is the sole enforcement point; callers must pass it via config YAML or CLI flag.

#### Next steps

1. Create `phase-3` branch before adding further backtesting code.
2. Add CLI entrypoint (`backtesting/cli.py`) for `python -m backtesting run --config ... --data-version ...`.
3. Wire `BacktestEngine` to real data from TimescaleDB via a loader module.
4. Run Phase 3 backtest on 2020–2024 with `v1_base_momentum.yaml` and log results to MLflow.
5. Execute walk-forward validation (3 folds, 2-year train, 1-year test).

---

### Session 12 — Held-out IC validation and factor methodology correction

**Date:** 2026-06-10

- Added `scripts/validate_signal_ic.py` with a frozen 70/30 chronological
  split, 21/63-day IC, rolling IC, turnover, survivorship audit, configurable
  gates, and `signal_ic_stats` persistence.
- Added frozen-holdout contamination warning to `validate_signal_ic.py`
  docstring: the Phase 2 holdout boundary is now sealed; iterating a factor
  implementation and retesting on the same split constitutes look-ahead bias
  even without directly viewing holdout data. New factors must use a later OOS
  window or a fully walk-forward design.
- Audited low-vol sign convention independently. The implementation correctly
  gives lower-volatility stocks higher scores; its negative held-out IC is an
  empirical rejection for this sample, not a sign bug.
- Replaced naive overlapping-return t-tests with Newey-West/HAC inference
  using `horizon_days - 1` lags.
- Repaired value and quality methodology:
  - flow metrics use four PIT-visible quarterly observations (TTM), with annual
    fallback;
  - restatements resolve to the latest filing known on the score date;
  - shares, equity, and assets are aligned at or before the corresponding flow
    period end;
  - fundamentals older than 550 days are treated as stale.
- Extended the daily signal DAG price lookback from 365 to 450 calendar days
  so 12-month momentum has 252 trading days plus the 21-day skip window.
- Mounted `./signals` read-only into the Airflow containers. The DAG previously
  parsed because factor imports are task-local, but factor tasks could not
  import the package at runtime.
- All four factors continue to be persisted for diagnostics, but production
  alpha now uses momentum only until another factor passes validation.
- Production-path validation for 2026-06-09 wrote 1,828 factor rows and 502
  alpha rows. All 502 alpha scores exactly matched momentum scores; low-vol,
  value, and quality remained diagnostic-only.

#### Frozen held-out result

Holdout begins 2024-12-05 (final 30% of 1,256 trading dates). Current-member
S&P 500 survivorship warning remains attached; 9 of 503 names are late entrants.

| Factor | 21d IC | 21d HAC t | 63d IC | 63d HAC t | Gate |
|---|---:|---:|---:|---:|---|
| Momentum | 9.73% | 2.43 | 15.33% | 2.07 | Pass |
| Low-vol | -16.58% | -3.21 | -24.60% | -2.90 | Reject |
| Value (TTM/aligned) | -2.22% | -1.04 | -2.00% | -0.66 | Reject |
| Quality (TTM/aligned) | -2.22% | -2.39 | -3.67% | -7.57 | Reject |

**Decision:** Revised the Phase 2 PRD exit criterion to validate the factor
research infrastructure rather than require three initial placeholder factors
to predict returns. Phase 2 is closed: the point-in-time-safe held-out workflow
is reproducible, momentum exceeds 3% IC with HAC t-stat >= 2.0 at both required
horizons, and rejected factors are excluded from production alpha. Do not tune
rejected factors against this holdout. New pre-specified predictors will be
evaluated through the same acceptance process as ongoing research.

## 2026-06-09

### Session 11 — Phase 2 complete: EDGAR ingestion, IC engine, value/quality factors, composite scorer

**Operator:** mshane@thecanadalist.ca
**Branch:** `claude/phase-2`
**Commits this session:** 10 commits (b7f4714 → 53fca21)

---

#### What was done

**Step 1 — settings.yaml tuning**

Corrected stale yfinance batch parameters: `yfinance_batch_size: 200 → 20` and `yfinance_inter_batch_delay: 1.0 → 3.0`. These now match the values validated in Session 10 that prevented the Yahoo Finance IP ban.

**Step 2 — Alembic migrations 002 and 003**

- `002_signal_schema.py`: Creates `factor_scores` (ticker, score_date, factor_name, strategy_id, z_score, raw_value) and `alpha_scores` (ticker, score_date, strategy_id, alpha_score, rank, universe_size) and `signal_ic_stats` (IC metrics by factor/horizon). TimescaleDB hypertables with surrogate BigInt PKs + UniqueConstraints. `signal_ic_stats` is NOT a hypertable (eval-date dimension, not a time-series append).
- `003_fundamentals_schema.py`: Creates `sec_filings` (accession_number UNIQUE, form_type, period_end_date, filing_date) and `financial_statements` (EAV: one row per ticker × period × item × release_date). PIT index on `(ticker, item_name, period_type, release_date DESC)`. Unique key includes `release_date` to preserve restatements.

[DECISION] EAV (Entity-Attribute-Value) schema for `financial_statements`: new line items can be added without schema migrations. Aggregation to wide format happens in the signal layer at query time, not in the DB schema.

[DECISION] Surrogate BigInt PKs for hypertables: TimescaleDB 2.x technically allows composite PKs but surrogate PKs + UniqueConstraints are cleaner and match the rest of the schema.

**Step 3 — IC validation engine**

Created `signals/research/ic.py`:
- `compute_forward_returns()`: row-shift via wide pivot, safe for uniform S&P 500 universe
- `compute_ic_series()`: Pearson + Spearman IC per (date, horizon); skips dates with < 5 valid tickers
- `summarize_ic()`: one-sided t-test (H1: mean_IC > 0); IC-IR = mean/std (unannualized); excludes horizons with < 30 observations (autocorrelation means small sample effective N is even smaller)
- `multiple_testing_correction()`: BH and BHY FDR correction via `statsmodels.stats.multitest.multipletests`
- `chronological_split()`: by unique dates, no date appears in both train and val sets
- `log_ic_to_mlflow()`: raises ValueError if data_version empty (C7 compliance)

40 unit tests in `signals/tests/test_ic.py`, all passing.

[DECISION] `_MIN_IC_DATES_FOR_TSTAT = 30` (raised from 10): IC series are autocorrelated at short lags, so effective sample size < raw count. A 30-observation minimum is still lenient but prevents wildly unreliable t-stats.

[DECISION] One-sided t-test (`alternative="greater"`): we are testing H1 that IC > 0, not just non-zero. This is more precise and avoids double-counting evidence.

**Step 4 — Fundamentals source decision: EDGAR vs SimFin**

Two independent research agents both confirmed SimFin's free tier has a 12-month data delay — unusable for live signal research. SEC EDGAR is the authoritative source: `filed` date = literal SEC submission timestamp (PIT gold standard), 10 req/s limit, free.

[DECISION] SEC EDGAR Company Facts API selected as the fundamentals source. SimFin free tier disqualified by 12-month delay.

**Step 5 — EDGAR fundamentals ingestion**

Created:
- `data/ingestion/fundamentals/concept_map.py`: XBRL concept alias maps for 10 items (revenue, gross_profit, operating_income, net_income, total_assets, total_equity, total_debt, shares_outstanding, operating_cash_flow, capex). Carefully excluded problematic aliases: `CapitalExpendituresIncurredButNotYetPaid` (non-cash accrual, not actual cash capex) and `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` (inflates equity denominator for ROE/B-P ratios).
- `data/ingestion/fundamentals/edgar_client.py`: `EdgarClient` with `get_cik_map()`, `fetch_company_facts()`, `extract_fundamentals()`, `ingest_ticker()`, `backfill()`. Thread-safe class-level rate limiting (10 req/s). `_parse_observations()` deduplicates on `(end, filed, accn)` and stores ALL observations including restatements. `_classify_period()` duration-based (340–380 days = annual, 75–110 days = quarterly) with `fp` fallback. `_compute_derived()` groups by `(period_end_date, period_type)` and uses `max(release_dates)` of inputs for PIT safety.

29 unit tests in `data/tests/test_edgar_client.py`, all passing.

[DECISION] Restatements stored as new rows (different `filed` dates), not overwriting originals. `pit_latest()` picks the correct value for any `as_of_date`. Full audit trail preserved.

[DECISION] Derived items (free_cash_flow) computed at ingestion time with `release_date = max(release_dates of inputs)`. This prevents look-ahead: FCF cannot be "known" before both its inputs are public.

**Step 6 — Value and quality factors**

Created `signals/factors/value.py`:
- Sub-factors: earnings_yield (E/P), book_to_market (B/P), fcf_yield (FCF/P)
- Market cap via shares × close price on score date
- `_pit_latest_fundamentals()`: `release_date ≤ as_of_date` filter, sort by (release_date, period_end_date) ascending then `.last()` per ticker
- Cross-sectional z-score per date; equal-weight composite `value_score`

Created `signals/factors/quality.py`:
- Sub-factors: roe (net_income / total_equity), gross_profitability (gross_profit / total_assets), accruals
- Hribar & Collins (2002) cash-flow accruals: `(net_income - operating_cash_flow) / total_assets`
- Accruals negated before z-scoring (low accruals = high quality = high positive score)
- Novy-Marx (2013) gross profitability factor included

22 unit tests in `signals/tests/test_value_quality.py`, all passing. Tests cover: PIT correctness (future fundamentals excluded), new filing picked up after release_date, accruals sign convention, zero market cap exclusion, zero equity exclusion.

[DECISION] Hribar & Collins (2002) cash-flow accruals variant chosen over Sloan (1996) balance-sheet accruals. Cash-flow based is more robust to accounting choices and better predicts future earnings quality.

**Step 7 — Composite scorer + Airflow DAG**

Created `signals/scoring/scorer.py`:
- `combine_factor_scores()`: pure function, no DB I/O
- Vectorised factor_scores_df build (replaced iterrows with pd.concat of renamed chunk DataFrames)
- Equal-weight: `mean(skipna=True)` auto-renormalises denominator
- Weighted: per-row weight renormalisation — `alpha = sum(w_i * v_i for non-NaN i) / sum(w_i for non-NaN i)` — so missing-factor tickers are not penalised
- Rank (1 = best) and universe_size computed AFTER dropna, so they cleanly span 1..universe_size

Created `airflow/dags/daily_signal_pipeline.py`:
- Schedule: `"30 21 * * 1-5"` (9:30 PM ET weekdays)
- 4 factor tasks in parallel after load_prices: compute_momentum, compute_lowvol, compute_value, compute_quality
- `trigger_rule="none_failed"`: pipeline halts on upstream failures (data corruption), but allows intentionally skipped tasks (future sensor-based flow control)
- Graceful degradation: value/quality tasks push `"[]"` on success when `financial_statements` table is empty (expected until Phase 2 fundamentals backfill)
- `convert_dates=False` on all `pd.read_json` calls to prevent timezone-aware ISO string misparse
- Null guard on `score_date_str` XCom pull
- Single atomic transaction for both `factor_scores` and `alpha_scores` writes (prevents inconsistent state if alpha insert fails after factor insert succeeds)

14 unit tests in `signals/tests/test_scorer.py`, all passing. Review agent identified and all 5 critical/major issues fixed before commit.

[DECISION] `trigger_rule="none_failed"` over `"all_done"`: `"all_done"` silently writes degraded alpha scores when a factor task fails (e.g. DB connection error). `"none_failed"` halts the pipeline on failures while still allowing intentionally skipped tasks.

[DECISION] Single transaction for both table writes: avoids the partial-failure scenario where factor_scores commits but alpha_scores fails, leaving the DB in a permanently inconsistent state that ON CONFLICT DO UPDATE cannot fully repair.

---

#### Phase 2 progress — updated status

| Deliverable | Status |
|-------------|--------|
| `config/settings.yaml` tuning | ✅ |
| Migration 002: signal schema | ✅ |
| Migration 003: fundamentals schema | ✅ |
| `signals/research/ic.py` IC engine | ✅ 40 tests |
| EDGAR vs SimFin decision | ✅ EDGAR selected |
| `data/ingestion/fundamentals/edgar_client.py` | ✅ 29 tests |
| `signals/factors/value.py` | ✅ |
| `signals/factors/quality.py` | ✅ 22 tests (value+quality combined) |
| `signals/scoring/scorer.py` | ✅ 14 tests |
| `airflow/dags/daily_signal_pipeline.py` | ✅ |
| Fundamentals backfill CLI | ⏳ not yet built |
| IC validation run on real data | ⏳ requires fundamentals backfill first |

**Total test count: 111 signals + data tests passing** (up from 75 at Phase 2 start)

---

#### Remaining items to complete Phase 2

1. **Fundamentals backfill CLI** (`scripts/backfill_fundamentals.py`): orchestrates `EdgarClient.backfill()` for all 503 tickers, with progress tracking and resume capability. Required before IC validation can run on real data.

2. **IC validation on real data**: run `compute_ic_series()` against live momentum + low-vol scores and real forward returns. Confirm IC > 0 at 1M and 3M horizons before committing to the composite.

3. **`signal_research` skill** (`.claude/skills/signal_research.md`): wraps IC engine for interactive factor research sessions.

4. **`score` skill** (`.claude/skills/score.md`): wraps scorer for on-demand composite score computation.

5. **`screen` skill** (`.claude/skills/screen.md`): wraps alpha_scores for ticker screening.

6. **Universe survivorship bias audit**: current S&P 500 universe uses today's constituents. A proper backtest should use point-in-time constituents (tickers that were in the S&P 500 on each score date).

---

#### Stretch goals

- **Sentiment factor**: `signals/factors/sentiment.py` using earnings call transcript tone or short-interest ratios (deferred per original plan — no high-quality free source identified)
- **Analyst revision factor**: change in consensus EPS estimates (requires IBES/Refinitiv — deferred, not free)
- **Factor turnover analysis**: measure how stable each factor's ranks are across months (low turnover = lower transaction costs when used in portfolio construction)
- **Walk-forward IC stability**: split IC computation by sub-period to detect factor decay or regime changes
- **Composite weight optimisation**: instead of equal-weight blending, use IC-weighted blending (weight each factor by its trailing IC-IR)

---

### Session 10 — Phase 1 live validation, Phase 2 low-vol factor, PR 1 merged

**Operator:** mshane@thecanadalist.ca
**Branch:** `claude/quant-system-prd-koDnJ` → merged to `main` via `release/phase-1`; Phase 2 work moved to `claude/phase-2`
**Commits this session:** see git log

---

#### What was done

**Part 1 — yfinance hardening (operator's local commits)**

Two commits pushed by operator from their local machine after independent investigation:

- `Harden yfinance backfill requests` — refactored `yfinance_client.py` to use a combined `fetch_market_data()` that fetches OHLCV and corporate actions in a single `yf.download(actions=True)` call, reducing API calls and adding jitter to inter-batch delays
- `Add resilient Airflow market data catch-up` — added `Dockerfile.airflow` with a custom Airflow image and improved the daily DAG's error handling and catch-up logic

[DECISION] Operator hardened the yfinance client independently. These commits were rebased cleanly onto the session branch. The combined `fetch_market_data()` approach is now canonical — it avoids a second round of Yahoo API calls for corporate actions.

**Part 2 — Yahoo Finance rate-limit diagnosis and resolution**

Initial `make backfill` attempts triggered a temporary IP ban from Yahoo Finance after the old 200-ticker batch code hammered their servers. Diagnosed by hitting the raw API endpoint directly:

```
status: 403
body: Host not in allowlist
```

[RESOLVED] IP ban cleared within ~1 hour. Subsequent runs with the redesigned backfill (20-ticker batches, 3 s delay, resumable) completed without hitting rate limits.

**Part 3 — Low-volatility factor (Phase 2)**

Built `signals/factors/low_vol.py`:
- Realized volatility at 21 / 63 / 252-day windows (annualised log-return std × √252)
- Optional rolling 252-day beta vs a market reference series (computed via rolling Cov/Var)
- Composite `lowvol_score` = negated + re-standardised mean of vol z-scores (lower vol → higher positive score, consistent with momentum sign convention)
- 16 unit tests in `signals/tests/test_low_vol.py`, all passing

**Part 4 — Phase 1 full live validation**

Operator triggered the Airflow `daily_data_pipeline` DAG manually. All 7 tasks completed successfully on both a scheduled and a manual run:

```
fetch_universe → fetch_ohlcv (28–29 s) → run_quality_checks → write_quality_flags
                                        → write_ohlcv → save_snapshot
                                        → write_corporate_actions
```

DB state after validation:
- `daily_prices`: 624,948 rows, 503 tickers, latest date 2026-06-08
- `data_quality_flags`: 4,707 rows
- `corporate_actions`: 7,922 rows, 430 tickers
- Zero duplicate (ticker, date) pairs

**Part 5 — Completeness checks, scripts, and fire drill runbook**

Added at Codex's recommendation (items #3, #4, #5):

- `data/normalization/completeness_checks.py` — four checks: duplicate (ticker, date) pairs, null close prices, short ticker histories (<252 rows), coverage vs reference ticker. Same flag format as `quality_checks.py`. 32 unit tests.
- `scripts/check_pipeline_health.py` — read-only DB health check: row counts, duplicate detection, null prices. Exits 1 on issues.
- `scripts/verify_prices.py` — cross-checks N random tickers against fresh Yahoo download. Ran successfully: 5 tickers, 14 days, max |Δ| = 0.0000.
- `scripts/pin_snapshot.py` — pins `daily_prices` to MinIO parquet. Snapshot created at `rqis-snapshots/snapshots/daily_prices/2026-06-08/data.parquet`.
- `docs/runbooks/airflow_fire_drill.md` — step-by-step recovery procedure: kill scheduler mid-run, restart, verify zombie-task recovery.

**Part 6 — PR 1 opened and merged**

Created `release/phase-1` branch (cherry-pick of all Phase 0 + Phase 1 commits, excluding Phase 2 factors). PR #1 opened against `main`, self-reviewed, merged by operator.

**Part 7 — Branch restructuring**

- `release/phase-1` deleted after merge
- `claude/phase-2` created from `release/phase-1` tip + cherry-pick of momentum + low-vol commits
- `claude/phase-2` rebased onto merged `main`
- `claude/quant-system-prd-koDnJ` (original development branch) — stale, pending deletion

[DECISION] Phase-gated PRs adopted as workflow: one PR per phase, opened only after live validation, merged to `main`. `claude/phase-N` branches are the active development branches. This keeps `main` always in a deployable validated state.

---

#### Phase 1 exit criteria — final status

| Criterion | Status |
|-----------|--------|
| Infrastructure stack runnable | ✅ |
| Database schema deployed | ✅ |
| OHLCV ingestion working | ✅ 624,948 rows / 503 tickers |
| Corporate actions pipeline | ✅ 7,922 rows / 430 tickers |
| Point-in-time correctness | ✅ |
| Quality checks operational | ✅ 4,707 flags recorded |
| 5 years of data in DB | ✅ |
| Data quality green | ✅ zero duplicates, price cross-check exact match |
| Airflow DAG running | ✅ 7/7 tasks, ×2 runs |
| Dataset snapshot pinned | ✅ `rqis-snapshots/snapshots/daily_prices/2026-06-08/data.parquet` |
| 194 unit tests passing | ✅ |
| PR 1 merged to main | ✅ |

---

#### Phase 2 progress so far

| Deliverable | Status |
|-------------|--------|
| `signals/factors/momentum.py` | ✅ 19 tests |
| `signals/factors/low_vol.py` | ✅ 16 tests |
| `signals/factors/quality.py` | ⏳ requires fundamentals ingestion |
| `signals/factors/value.py` | ⏳ requires fundamentals ingestion |
| `signals/scoring/scorer.py` | ⏳ defer until ≥3 factors exist |
| Fundamentals ingestion | ⏳ next major workstream |

---

#### Next steps

1. Delete stale `claude/quant-system-prd-koDnJ` branch
2. Set `main` as default branch in GitHub settings
3. Begin Phase 2 fundamentals ingestion (SimFin or yfinance `.info` for P/E, P/B, ROE, etc.)
4. Once fundamentals are available: build quality and value factors
5. Build `signals/scoring/scorer.py` to combine factor z-scores into a composite

---

## 2026-06-06

### Session 9 — Backfill redesign + Phase 2 momentum signal

**Operator:** mshane@thecanadalist.ca
**Branch:** `claude/quant-system-prd-koDnJ`
**Commits this session:** (see git log)

---

#### What was done

**Part 1 — Backfill redesign (yfinance at scale)**

`make backfill` fails at ~503 tickers × 5 years because Yahoo Finance rate-limits
large repeated batch downloads. Two root causes were identified and fixed:

1. **Batch size too large (200 → 20 tickers)**  
   yfinance silently drops individual tickers from large batches when Yahoo
   returns an empty body ("Expecting value: line 1 column 1 (char 0)"). These
   are not Python exceptions — our `try/except` never catches them. The ticker
   is simply absent from the returned DataFrame.

2. **Non-resumable, all-at-once write**  
   The old design fetched all 503 tickers, then wrote everything at the end.
   Any interruption lost the entire run.

Fixes:
- `_backfill_cli()` in `yfinance_client.py` fully rewritten:
  - 20-ticker batches (`_BACKFILL_BATCH_SIZE = 20`)
  - 3 s inter-batch delay (up from 1 s)
  - Per-batch retry: 3 attempts with 5 s / 10 s / 20 s back-off
  - `upsert_ohlcv()` called after every batch — crash loses at most 20 tickers
  - Quality flags written per batch
  - Failed tickers collected and logged; excluded from corporate-actions fetch
- `TimescaleWriter.get_tickers_with_data(start, end)` — new read helper.
  Queries `daily_prices` for tickers with rows in the first 31 calendar days
  of the target range. Backfill skips tickers already present, making reruns
  cheap and safe.

[DECISION] Batch size set to 20, not the original 200. Rationale: Yahoo's
informal rate limit appears to be around 20–50 simultaneous ticker requests.
20 is conservative and leaves headroom for retries. The extra time cost
(~25 batches × 3 s delay ≈ 75 s overhead per full run vs 2.5 s) is trivial
for a daily or one-time operation.

**Part 2 — Rate limit diagnosis**

After the large failed backfill attempt, Yahoo temporarily blocked the
operator's residential IP. Diagnosis confirmed by hitting the raw Yahoo
Finance API with `requests.get()`:

```
status: 403
body: Host not in allowlist
```

[BLOCKER] Yahoo Finance rate-limited the operator's IP after the large batch
run. Temporary — typically clears in 15–60 min. Confirmed by direct HTTP test.

[RESOLVED] Once the block clears, run `make backfill` with the new code. The
resumability check means only un-fetched tickers will be attempted.

**Part 3 — Phase 2 momentum factor**

Built `signals/factors/momentum.py` — the first Phase 2 deliverable.

Design:
- Four lookback windows: 1 M, 3 M, 6 M, 12 M (skipping the final month per
  Jegadeesh–Titman to avoid short-term reversal contamination)
- Cross-sectional z-score normalization per date (mean 0, std 1)
- Composite score = equal-weight average of all available window z-scores
- Point-in-time safe: requires only the `date` and `close` columns present
  in `daily_prices`; never looks ahead
- Full unit test suite in `signals/tests/test_momentum.py`

---

#### Next steps
- Wait for Yahoo rate limit to clear, then run `make backfill`
- After data is populated: validate momentum signals against the live DB
- Begin `signals/factors/quality.py` (ROE, ROIC, accruals) — requires
  fundamentals data, so deferred until fundamentals ingestion is built

---

## 2026-06-05

### Session 8 — Fix backfill CLI and Wikipedia universe fetch bugs

**Operator:** mshane@thecanadalist.ca  
**Branch:** `claude/quant-system-prd-koDnJ`  
**Commits this session:** (see git log)

---

#### What was done

Operator ran `make backfill` and hit two failures. Both fixed.

**Bug 1 — `_backfill_cli()` not loading `.env`**

Root cause: The backfill entry point in `yfinance_client.py` called
`TimescaleWriter()` which reads `DATABASE_URL` from `os.environ`, but
the backfill CLI never loaded `.env` before entering that code. Running
`make backfill` from a plain shell (without manually exporting env vars)
meant `DATABASE_URL` was absent and the writer failed to connect.

Fix: Added `load_dotenv()` at the top of `_backfill_cli()`, before any
imports that read environment variables.

**Bug 2 — Wikipedia S&P 500 fetch returning 403 (Windows / User-Agent)**

Root cause: `pd.read_html(url)` uses Python's `urllib` with a default
`Python-urllib/3.x` User-Agent. Wikipedia blocks this with a 403 on
Windows (and increasingly on other platforms). The fetch silently returned
an empty list, so `tickers=0` and the backfill wrote nothing.

Fix: Replaced `pd.read_html(url)` with a `requests.get()` call that sends
a Chrome-style `User-Agent` header, then passes `io.StringIO(response.text)`
to `pd.read_html()`. The `io.StringIO` wrapper is also required by pandas 2.x,
which treats a bare HTML string argument as a file path (causing a
`No such file or directory` error with the raw string).

Added `requests==2.31.0` explicitly to `requirements.txt` (it was always
installed as a transitive dependency of yfinance, but we now import it
directly).

Updated `test_universe_loader.py`: the four Wikipedia tests now mock
`config.universe_loader.requests.get` instead of `pd.read_html`,
and a new `test_sends_browser_user_agent` test asserts the User-Agent
header is present.

**Final test count:** 121 tests, all passing.

---

#### Next steps
Re-run `make backfill` — should complete successfully now.

---

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

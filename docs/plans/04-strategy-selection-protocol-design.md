# 04 — Strategy-Selection Protocol Design Plan

**Chat session ID:** `8d75cb10-3224-4e06-9ae2-4b9f45d1d4b8`

**Roadmap task:** Establish a real strategy-selection protocol (04 — Research
qualification, P1, XL)
**Scope:** Governance/process layer binding the existing statistical
instruments in `backtesting/validation/` into a disciplined,
auditable, fail-closed selection pipeline. No new statistics are invented;
no strategy is selected or requalified by this document.
**Status:** Design plan — Phase 0 deliverable only. No code, migration, or
`bugs.md`/`Roadmap.md` change ships with this document. Implementation is
phased 04-1 through 04-6 (§7) and assigned to future builder sessions,
contingent on operator answers to §8.

## 1. Problem statement and scope

Three prerequisite gates are delivered and merged: PIT-complete, immutable,
content-addressed research data (03A), execution/analytic price-series
separation (03B), and fail-closed strategy-config validation (02B). The
statistical instruments that should sit on top of that foundation already
exist — walk-forward validation, a survival funnel, a parameter-sensitivity
sweep, a permutation stress test, and Deflated-Sharpe/FDR corrections. What
does not exist is the **governance process** that makes those instruments
trustworthy as a selection mechanism rather than decoration:

- nothing durably records every strategy variant an operator or builder ever
  tried, so `deflated_sharpe_ratio(n_trials=...)` can only ever be called
  with a hand-typed, unverifiable guess;
- nothing prevents a selection decision from having peeked at out-of-sample
  or held-out data before making that decision;
- nothing freezes a strategy's configuration at the moment of selection in a
  way that is enforced rather than merely conventional;
- nothing orchestrates the existing instruments into one promotion decision
  that is itself recorded as evidence.

**In scope:**

- A durable hypothesis/trial registry that records every backtest-shaped
  candidate run against a strategy, feeding an honest `n_trials` into the
  multiple-testing correction.
- Enforced train / out-of-sample (OOS) / locked-final-holdout date
  partitioning, with the holdout mechanically sealed until a one-shot final
  confirmation run.
- A config-freeze mechanism tying strategy selection to the existing
  Strategy Registry's canonical-config-hash/C6 discipline.
- An end-to-end promotion pipeline that orchestrates survival funnel →
  parameter sensitivity → bootstrap stress → Deflated Sharpe/FDR (with
  `n_trials` sourced from the trial registry) and yields a recorded
  promotion decision plus an evidence bundle, integrated with the Strategy
  Registry lifecycle and MLflow.
- New Alembic migration(s) for the trial registry and any lifecycle status
  addition (C2).

**Explicitly deferred (non-goals):**

- Redesigning or replacing any of the existing statistical instruments
  (`survival_funnel.py`, `parameter_sensitivity.py`, `bootstrap_stress.py`,
  `overfitting_checks.py`, `walk_forward.py`, `indicator_diagnostic.py`).
  This plan wires them together; it does not touch their internals.
- Market regime detection / strategy-mix switching (roadmap row "Market
  regime detector" — explicitly sequenced *after* this gate identifies the
  hardened strategy set).
- Calibrating backtest cost/participation assumptions against paper-trading
  reality (roadmap row "Calibrate research against production reality" —
  09, a separate downstream gate that depends on this one).
- Fixing BUG-066 (cross-sectional minimum-eligible-count gate), BUG-068
  (Wikipedia constituent count drift), or BUG-071 (score-leg same-session
  cutoff edge case). This protocol is designed to operate correctly *given*
  those residual limitations — see §9 for how each is acknowledged and
  bounded rather than silently ignored.
- A general-purpose experiment-tracking UI. Trial registry records surface
  through the existing Strategy Registry CLI/DB and MLflow; a dashboard view
  is deferred to M5.8 per the existing roadmap sequencing.
- Live-capital decisions of any kind (C8's 4-week paper qualification and
  the independent live-readiness review remain separate, later gates).

## 2. Inventory of existing tooling

All of the following already exist and are **not** rebuilt by this plan;
the protocol's job is to call them in the right order, with the right
inputs, and to record that it did so.

| Instrument | File | What it already does |
|---|---|---|
| Walk-forward validator | `backtesting/validation/walk_forward.py` (`WalkForwardValidator.run`) | Splits a date range into `n_folds` train/test windows (`expanding` or `rolling`), runs `BacktestEngine` per fold, returns `WalkForwardResult` with per-fold `BacktestResult`s, concatenated `oos_returns`, aggregate `oos_metrics`, and the exact `config` used. Fails closed via `validate_backtest_config` (02B) before any fold runs. |
| Survival funnel | `backtesting/validation/survival_funnel.py` (`SurvivalFunnel.check`) | Six configurable gates: `min_oos_sharpe` (0.5), `max_oos_drawdown` (-35%), `max_oos_sharpe` (2.5, lucky-artifact ceiling), `is_oos_consistency` (≤30% IS/OOS gap), `min_trade_count` (30), `positive_is_sharpe`. Returns a `SurvivalFunnelResult` with per-gate pass/fail and a verdict string. Helpers `avg_is_sharpe_from_wf`/`oos_trade_count_from_wf` adapt a `WalkForwardResult` into the gate inputs. |
| Parameter sensitivity sweep | `backtesting/validation/parameter_sensitivity.py` (`ParameterSweeper.sweep`) | Runs `WalkForwardValidator` across the Cartesian product of a `param_grid` of dot-path overrides. Reports `mean_oos_sharpe`, `std_oos_sharpe`, `positive_fraction`, and a `robust`/`curve_fit` verdict (default: fails if `positive_fraction < 0.5` or `std_oos_sharpe > 0.5`). Per-variant engine/data errors are caught and recorded as NaN rows without aborting the sweep; config errors (`UnsupportedStrategyConfigError`) always propagate and halt it. |
| Bootstrap/permutation stress test | `backtesting/validation/bootstrap_stress.py` (`bootstrap_stress`) | Reshuffles (permutes, not resamples) a strategy's OOS daily-return sequence `n_reshuffles` times (default 500) and reports the resulting max-drawdown distribution (`drawdown_p5/p50/p95`, `worst_case_drawdown`) plus a `solid`/`fragile` verdict against `fragile_drawdown_threshold` (default -35%). Correctly documents that Sharpe/CAGR are permutation-invariant, so only drawdown is reported. |
| Deflated Sharpe Ratio + multiple-testing corrections | `backtesting/validation/overfitting_checks.py` | `deflated_sharpe_ratio(observed_sharpe, n_trials, n_observations, sharpe_std, risk_free_rate)` (Bailey & López de Prado 2014, with Lo 2002 SR-variance correction); `bonferroni_correction(p_values, alpha)`; `benjamini_hochberg(p_values, fdr)`; `minimum_track_record_length(...)`. **`n_trials` is a caller-supplied `int` with no wiring to any durable count anywhere in the repository** (confirmed by repo-wide grep — the only references to these functions are inside this module itself; no script or registry call site exists yet). |
| Indicator diagnostics | `backtesting/validation/indicator_diagnostic.py` | Per-indicator IC/robustness diagnostics feeding into signal-level research decisions upstream of strategy-level backtests (not modified by this plan; a strategy config's indicator choices are themselves inputs the trial registry records, not re-derived). |
| MLflow backtest logging | `backtesting/experiment_tracking/mlflow_logger.py` (`BacktestLogger.log_run`, `.log_walk_forward_run`) | Already enforces C7 (non-empty `data_version`), validates config via `validate_backtest_config` (02B), verifies config-hash provenance between the passed config and the actual `BacktestResult`/`WalkForwardResult` (`ConfigProvenanceMismatchError`), and — since 03A-5 — can require a manifest-hash-shaped `data_version` via `require_manifest_data_version=True`. **Already accepts an optional `funnel_result: SurvivalFunnelResult`** and logs each gate as an MLflow tag (`gate.<name>` = `PASS`/`FAIL`, plus `survival_funnel.passed`/`.verdict`) in both `log_run` and `log_walk_forward_run`. It does **not** currently accept a bootstrap-stress or DSR/FDR result as a first-class parameter on `log_run` (only `log_walk_forward_run` accepts an optional `stress_result: BootstrapStressResult`); neither method accepts anything DSR/FDR-shaped today. |
| Strategy Registry | `strategy_registry/` (`registry.py`, `models.py`, `fingerprint.py`, `cli.py`); spec in `docs/strategy_registry_spec.md` | DB-backed catalog with two-level identity (`strategy_definitions` = research/config history, `strategies` = operational lifecycle). Canonical-config-hash fingerprinting excludes only `data_version` from the hash (`_RUNTIME_KEYS = {"data_version"}` in `fingerprint.py`), so any factor/portfolio/execution change produces a new hash — this is the exact C6 freeze mechanism this plan reuses. `record_run()` already appends append-only `strategy_runs` rows (`run_type IN ('unit','signal_ic','backtest','walk_forward','paper','live')`) keyed to `(strategy_id, config_hash)`, requires `data_version` for `backtest`/`walk_forward` run types (C7), and can be called **before** formal registration. `transition()` enforces the lifecycle state machine and one-active-per-status DB partial unique indexes. **Confirmed by direct inspection of `strategy_registry/models.py`: the current status CHECK constraint is `status IN ('backtesting', 'paper', 'live', 'archived')` — there is no `VALIDATED` status today.** This is a gap this plan must resolve (§3, §5). |
| Config fail-closed contract | `backtesting/config_contract.py` (`validate_backtest_config`) | Rejects any strategy config field/section/value the backtest path does not implement, at all 6 backtest entry points (02B/BUG-075). Reused unchanged by every instrument above; the promotion pipeline inherits this for free by calling those instruments rather than the engine directly. |
| Research methodology identity | `data/research/models.py` (`ResearchMethodology`, `ResearchRun`); migration `012_research_identity.py` | Versions the *data-timing* policy (universe import policy, score/realized-return corporate-action availability policy, missing-data policy, code/config hash) a score or backtest was computed under. `ResearchRun.data_version` + `.methodology_id` is the existing hook this plan's trial registry links against, rather than inventing a parallel data-identity scheme. |
| PIT eligibility/manifest content-addressing | `data/universe/runtime.py`, `backtesting/dataset_manifest.py`, `data/storage/parquet_snapshots.py` (03A) | `manifest_content_sha256` is the enforced C7 `data_version` shape (`require_manifest_hash_data_version`); a bundle's `eligibility_batch_id`/`membership_import_batch_id`/`research_methodology_id` are already linked. This plan's train/OOS/holdout split (§4) is a *date-range* partition layered on top of this data — it does not change how the underlying bundle is fetched or verified. |

## 3. Gap analysis (verified against code)

### Gap 1 — No durable hypothesis/trial registry

**Verified.** `overfitting_checks.deflated_sharpe_ratio` takes `n_trials: int`
with no default and no lookup — the only call sites in the repository are
inside `overfitting_checks.py`'s own docstring examples. `strategy_runs`
(Strategy Registry) records individual runs, but nothing counts *distinct
parameter/config variants explored in pursuit of a single strategy_id* as a
first-class, queryable trial count, and nothing prevents a builder from
running ten walk-forwards, discarding nine, and reporting `n_trials=1` to
the DSR calculation for the tenth. `ParameterSweeper.sweep` produces
`configs_tested` (an `int` on `ParameterSensitivityResult`) for a single
*sweep invocation*, but a strategy's true trial count spans every sweep,
every manual walk-forward, and every ad hoc parameter tweak ever run against
it — including ones that were not part of a formal sweep object at all.
**Design response:** §4.1 below (durable `strategy_trials` table, populated
automatically by the promotion pipeline's own call sites rather than by
hand-entry).

### Gap 2 — No enforced data-split governance

**Verified.** `WalkForwardValidator.run` takes `config["backtest"]["start_date"
]`/`["end_date"]` and subdivides them into folds *within that single caller-
supplied range* — there is no concept of a global, cross-strategy "OOS
window" or "final holdout window" that a caller is mechanically prevented
from including in a training run. Nothing stops a builder from setting
`end_date` to include what should be sealed holdout dates while iterating on
parameters, and nothing distinguishes "OOS data used during selection
iteration" (legitimate walk-forward OOS folds, used to *compare* candidates)
from "final holdout data used exactly once to confirm the already-selected
winner" (must never be touched during iteration). **Design response:** §4.2
(a `research_data_windows` policy record plus a runtime guard that rejects a
backtest/walk-forward date range overlapping the registered holdout window
unless a one-shot "final confirmation" flag is set and has not been
previously consumed for that strategy).

### Gap 3 — No config-freeze binding to selection

**Verified.** The Strategy Registry already gives C6-grade config freezing
*once a strategy is registered* (`register()` creates the `strategies` row
pinning `canonical_config_hash`; `verify_config_integrity()` detects drift).
But nothing today requires that a promotion decision be made *before* — or
strictly gated on — that freeze: a builder could keep editing
`config/strategy/v3_foo.yaml` after informally deciding "this is the one,"
producing a new config hash with no record that the promotion evidence was
computed against a now-stale hash. **Design response:** §4.3 (the promotion
pipeline requires a `strategy_definitions` row — i.e., an already-fingerprinted,
immutable config — as its input, never a live YAML path; the evidence bundle
records the exact `config_hash` it evaluated, and `verify_config_integrity`-
style drift detection is re-run as the final promotion pre-check).

### Gap 4 — No end-to-end promotion gate

**Verified.** Each instrument in §2 is independently callable and
independently tested, but nothing composes `SurvivalFunnel` →
`ParameterSweeper` → `bootstrap_stress` → `deflated_sharpe_ratio`/
`benjamini_hochberg` into one ordered pipeline with a single pass/fail
promotion verdict, and nothing connects that verdict to the Strategy
Registry's lifecycle (there is no `VALIDATED` status — see §2's Strategy
Registry row — so today there is no lifecycle slot for "cleared the
statistical gates but not yet paper-trading" at all; a strategy can only be
`backtesting`, `paper`, `live`, or `archived`). **Design response:** §4.4/§4.5
(a `PromotionPipeline` orchestrator, a new `validated` status inserted
between `backtesting` and `paper`, and the migration to add it).

## 4. Proposed protocol

### 4.0 End-to-end flow

```
1. HYPOTHESIS RECORDED
   Operator/builder registers a strategy_id + a named research question
   (strategy_trials.hypothesis) before any candidate config is fingerprinted.
        │
        ▼
2. CANDIDATE CONFIGS FINGERPRINTED (existing: strategy_registry.add_definition)
   Each parameter variant becomes its own strategy_definitions row
   (strategy_id, config_hash). No DB write yet needed to "count" a trial --
   see step 3.
        │
        ▼
3. TRIALS RUN INSIDE THE TRAINING WINDOW ONLY
   Every WalkForwardValidator.run / ParameterSweeper.sweep call is wrapped by
   a TrialRecorder that:
     a. rejects any date range overlapping the registered holdout window
        (Gap 2 guard) unless final-confirmation mode is explicitly requested
        and unconsumed;
     b. inserts one strategy_trials row per (strategy_id, config_hash,
        run_type) BEFORE dispatching to the wrapped instrument, so a crashed
        or discarded run still counts (closes the "just don't report the
        bad ones" hole -- see Gap 1);
     c. on completion, updates that row with the observed OOS Sharpe/metrics.
        │
        ▼
4. SELECTION (operator/PM decision, inside the training+OOS window only)
   The operator picks a winning config_hash. This is a human decision;
   automation may recommend but never auto-selects (see open question 5).
        │
        ▼
5. CONFIG FREEZE
   registry.register() (or an existing strategies row's re-verification)
   pins the winning config_hash. verify_config_integrity() must pass.
        │
        ▼
6. PROMOTION PIPELINE (PromotionPipeline.run)
   Orchestrates, against the SAME frozen config_hash and its already-
   recorded trials:
     survival_funnel.check(...)
       -> parameter_sensitivity.sweep(...)   [re-verifies robustness at the
                                               frozen hash's neighborhood]
       -> bootstrap_stress(...)
       -> deflated_sharpe_ratio(n_trials=COUNT(*) FROM strategy_trials
                                  WHERE strategy_id=... )   [Gap 1 closed:
                                  n_trials is a query, not a hand-typed int]
       -> benjamini_hochberg(...) across all sibling strategy_ids' latest
          trial p-values, if a family-wise comparison is in play (open
          question 6)
   Produces a PromotionResult (pass/fail per stage + overall verdict) and an
   evidence bundle (JSON + MLflow run).
        │
        ▼
7. IF PASS: strategy transitions backtesting -> validated
   (new Strategy Registry status; §4.5). strategy_status_history gets an
   append-only row citing the PromotionResult's MLflow run_id.
        │
        ▼
8. FINAL HOLDOUT CONFIRMATION (one-shot, separate from step 6)
   A single, mechanically-gated run against the sealed holdout window.
   TrialRecorder marks the holdout window "consumed" for this strategy_id
   after this call; a second holdout run for the same strategy_id fails
   closed (§4.2). Result recorded as its own strategy_trials/MLflow run,
   tagged run_type='holdout_confirmation'.
        │
        ▼
9. validated -> paper transition (existing Strategy Registry mechanism,
   unchanged) proceeds only after step 8 passes. This is still gated by the
   existing paper-readiness commands in CLAUDE.md; this plan does not
   change those.
```

### 4.1 Trial registry

A new `strategy_trials` table (schema in §5) records **every** candidate
run — not just the ones a human chooses to keep. Population is automatic:
`TrialRecorder` (new module, `backtesting/validation/trial_recorder.py`)
wraps `WalkForwardValidator.run` and `ParameterSweeper.sweep` so a trial row
is inserted *before* the wrapped call executes (capturing crashed/discarded
runs) and updated with outcome metrics after. This directly answers Gap 1:
`n_trials` for `deflated_sharpe_ratio` becomes
`SELECT COUNT(*) FROM strategy_trials WHERE strategy_id = :sid AND
window = 'train_oos'`, not a hand-typed guess. A trial's `hypothesis_id`
column links back to a `strategy_hypotheses` row so the "we decided in
advance what we were testing" pre-registration discipline (not just the
raw run count) is itself queryable and auditable — closing the softer
version of Gap 1 (an honest count without an honest hypothesis is still
gameable by post-hoc rationalized parameter grids).

### 4.2 Data-split governance (train / OOS / locked final holdout)

A `research_data_windows` table (per `strategy_id`, or per `strategy_family`
for shared-window discipline across related strategies — see open question
1) records three non-overlapping date ranges: `train_start/end`,
`oos_start/end` (used by `WalkForwardValidator` folds during iteration —
this is *not* the sealed holdout; it is the "informative but not final"
comparison ground the survival funnel already treats as OOS), and
`holdout_start/end` (touched exactly once, in step 8). `TrialRecorder`
enforces this: any `WalkForwardValidator.run`/`ParameterSweeper.sweep`
whose effective date range (via `config["backtest"]["start_date"/"end_date"]`)
overlaps `holdout_start/end` is rejected with a new
`HoldoutWindowViolationError` unless the caller passes
`final_holdout_confirmation=True` **and** no prior `run_type=
'holdout_confirmation'` trial row exists for that `strategy_id` — the
one-shot seal. This is enforced in code (not just documentation) at the one
chokepoint (`TrialRecorder`) every promotion-pipeline caller is required to
go through; direct calls to `WalkForwardValidator.run` bypass the guard the
same way direct SQL bypasses an ORM constraint, so §4.4/§6 make
`TrialRecorder` the only sanctioned entry point for anything that will feed
a promotion decision.

**Invariant (04-2 rounds 2-4 hardening).** The guard must validate every
concrete date range whose data is actually READ during dispatch, using
inclusive-boundary semantics — it must never validate a declared/base range
that dispatch can diverge from. Date ranges are inclusive on both ends
(`DataHandler.trading_dates` returns `[start, end]`), so `effective_end`
must be strictly before `holdout_start`, not merely `<= oos_end` (a run
ending exactly on a touching `oos_end == holdout_start` boundary would
otherwise read the first sealed holdout session). A `ParameterSweeper.sweep`
`param_grid` may **not** override `backtest.start_date`/`backtest.end_date`
(or any other config key that controls which dates' data get read — the
config-contract audit found no others; every other CONSUMED field governs
strategy parameters, the cost model, or record labelling within an
already-fixed range): `TrialRecorder.run_parameter_sweep` rejects any such
`param_grid` before recording or dispatch, because a sweep varies STRATEGY
parameters only — the evaluation window is governed by the registered
`research_data_windows` row, never by the sweep grid. Walk-forward fold
subdivision needs no separate check: every fold date is drawn from
`data_handler.trading_dates(full_start, full_end)`, itself bounded to the
already-validated outer range, so no fold can exceed it.

### 4.3 Config freeze binding

The promotion pipeline's only valid input is a `(strategy_id, config_hash)`
pair that already exists as a `strategy_definitions` row (i.e., produced by
`registry.add_definition()`/`fingerprint()`), never a live YAML file path.
Before running, `PromotionPipeline` calls a config-drift check equivalent to
`verify_config_integrity()` against the *source* config file if the
strategy has already been formally `register()`-ed, and refuses to proceed
on drift. This guarantees the evidence bundle in step 6 is provably about
the exact bytes that will be (or already are) frozen — closing Gap 3.

### 4.4 Promotion pipeline

New `backtesting/validation/promotion_pipeline.py::PromotionPipeline.run(
strategy_id, config_hash, data_handler, holdout_mode=False) ->
PromotionResult`. Internally: re-runs (or reuses, if already recorded and
still within a configurable staleness bound — open question 7) a walk-forward
validation via `TrialRecorder`, then `SurvivalFunnel.check`, then
`ParameterSweeper.sweep` (using a *pre-declared* grid the strategy's
hypothesis specifies — an ad hoc grid invented after seeing the result is
not admissible, matching 03A/01B's "no tuning after seeing the holdout"
discipline extended one level earlier to standard OOS), then
`bootstrap_stress`, then `deflated_sharpe_ratio` with `n_trials` sourced from
§4.1's count. `PromotionResult` aggregates every stage's pass/fail plus a
single overall verdict and is both:

- persisted via `BacktestLogger.log_walk_forward_run(..., funnel_result=...,
  stress_result=...)` (already-existing parameters) **plus** a new,
  small logging addition (§6) so DSR/FDR values and the sourced `n_trials`
  are also recorded as MLflow tags/metrics (today only `funnel_result` and
  `stress_result` are first-class; DSR/FDR are not logged anywhere yet); and
- written to `strategy_trials`/a new `promotion_decisions` audit row (§5) so
  the Strategy Registry transition in step 7 can cite it by ID rather than
  by loose MLflow run-name convention.

**`n_trials` sweep-counting policy (04-4 decision, deferred from 04-2).**
04-2's `TrialRecorder.run_parameter_sweep` records exactly ONE
`strategy_trials` row per sweep *invocation*, not one row per grid variant,
because sweep variants aren't registered `strategy_definitions` rows and so
cannot satisfy `strategy_trials`' composite `config_hash` FK; the variant
count is preserved as `metrics_json['configs_tested']` on that single row.
`PromotionPipeline` (this section) must make a conscious choice of whether
its Deflated Sharpe `n_trials` count treats a recorded sweep as 1 trial or as
`configs_tested` trials. Given the operator's §8 Q3 resolution that DSR is
informational-only (not a hard promotion gate), this is not a
correctness-blocking issue today, but it must be decided deliberately here
rather than left implicit.

### 4.5 Strategy Registry integration: the `validated` status

Insert `validated` between `backtesting` and `paper` in the lifecycle:

```
backtesting → validated → paper → live
     │                       │       │
   archive                archive  step-down → paper
                                     │
                                   archive
```

`backtesting → validated`: allowed only via `PromotionPipeline` producing an
overall PASS (never a bare CLI status-transition call — enforced by
requiring `transition(to_status='validated')` to accept a mandatory
`promotion_result_ref` argument that must resolve to a real, passing
`promotion_decisions` row for that exact `strategy_id`/`canonical_config_hash`
pair). `validated → paper`: still requires the operator's existing paper-
readiness preflight chain (CLAUDE.md Steps 1-8); this plan does not touch
that chain, it only adds a new required prior state. `validated → archived`:
allowed (a strategy can clear statistics and still never be operationally
deployed). `validated → backtesting`: allowed as a step-down (e.g., an
operator later disputes the promotion evidence). This requires: (a) a new
CHECK-constraint value in `strategies.status` and
`strategy_status_history.to_status` (migration, §5); (b) new transition
rules in `strategy_registry/registry.py`'s transition table; (c) the new
mandatory `promotion_result_ref` parameter on the `backtesting→validated`
edge only (all other edges keep today's `operator_notes`-only behavior,
preserving existing call sites for those transitions unchanged).

## 5. Data model / schema changes

All new tables via Alembic migration (C2 — no raw DDL). Latest existing
migration is `013_universe_eligibility_attributes.py`; the next free
migration number is **`014`**.

### 5.1 Migration `014_strategy_selection_protocol.py`

**`strategy_hypotheses`** — pre-registered research questions, written
*before* any candidate config is run (enforces "select only inside training
windows" by making the intended comparison explicit up front, not
reconstructed after the fact).

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `strategy_id` | `TEXT NOT NULL` | Matches Strategy Registry's `strategy_id` format constraint; no FK required (a hypothesis can precede any `strategy_definitions`/`strategies` row) |
| `hypothesis_text` | `TEXT NOT NULL` | Free-text description of what is being tested and why |
| `param_grid_json` | `JSONB` | The pre-declared parameter-sensitivity grid (§4.4); frozen once trials begin — see `frozen_at` |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `frozen_at` | `TIMESTAMPTZ` | Set on the first linked `strategy_trials` row; `param_grid_json` becomes immutable at the application layer once non-null (enforced in `TrialRecorder`, not a DB trigger, matching the existing codebase's application-layer-enforcement style e.g. `verify_config_integrity`) |

**`strategy_trials`** — append-only (C3-style discipline; never
UPDATE/DELETE a terminal-status row, matching `strategy_runs`'s existing
pattern), one row per candidate run attempt.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `strategy_id` | `TEXT NOT NULL` | |
| `config_hash` | `TEXT NOT NULL` | FK → `strategy_definitions(strategy_id, config_hash)` ON DELETE RESTRICT — reuses the existing Strategy Registry definition row rather than duplicating config storage |
| `hypothesis_id` | `BIGINT` | FK → `strategy_hypotheses(id)` ON DELETE RESTRICT; nullable only for a documented legacy-backfill migration path, never for new rows (enforced in `TrialRecorder`, not the DB, to allow the one-time backfill of pre-protocol trials without a partial-NULL CHECK) |
| `window` | `TEXT NOT NULL` | `train_oos` \| `holdout` — which §4.2 window this trial's date range falls in |
| `run_type` | `TEXT NOT NULL` | `walk_forward` \| `parameter_sweep_variant` \| `holdout_confirmation` |
| `data_version` | `TEXT NOT NULL` | Manifest-hash-shaped C7 `data_version`, same enforcement as `require_manifest_hash_data_version` |
| `status` | `TEXT NOT NULL` | `running` \| `completed` \| `errored` (mirrors `strategy_runs.status` naming minus `blocked`, which does not apply to an automated trial) |
| `oos_sharpe` | `NUMERIC` | Nullable while `status='running'`. CORRECTION (2026-07-22 adversarial review): Postgres `numeric` DOES support `NaN` (`'NaN'::numeric` inserts and persists), so this column is NOT implicitly NaN-safe. Migration 014 adds a Postgres-only CHECK (`oos_sharpe IS NULL OR oos_sharpe <> 'NaN'::numeric`) that rejects NaN while allowing NULL; SQLite coerces `float('nan')` to NULL on storage (so the SQLite test path cannot persist NaN) and rejects the `::numeric` cast syntax, hence the CHECK is `ddl_if(postgresql)`. `oos_max_drawdown` and `promotion_decisions.dsr_value` carry the same backstop. Writers must additionally normalize non-finite floats to None before insert. |
| `oos_max_drawdown` | `NUMERIC` | |
| `metrics_json` | `JSONB NOT NULL DEFAULT '{}'` | Full metrics bag, mirrors `strategy_runs.metrics` |
| `mlflow_run_id` | `TEXT` | |
| `started_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `completed_at` | `TIMESTAMPTZ` | |

**Constraint:** `ck_strategy_trials_window`: `window IN ('train_oos',
'holdout')`. **Constraint:** `ck_strategy_trials_holdout_window_iff_confirmation`:
`("window" = 'holdout') = (run_type = 'holdout_confirmation')` — a row touches
the holdout window **if and only if** it is a `holdout_confirmation` run. This
couples the two columns so a holdout-window row cannot hide under a normal
`run_type` (which would otherwise escape the run_type-keyed seal below and
permit unlimited looks at the sealed holdout data); with the biconditional,
every holdout-window row is provably a `holdout_confirmation` row that the seal
covers. **Constraint:** at most one `run_type='holdout_confirmation'`
row per `strategy_id` **of any status** — a partial unique index
`uix_strategy_trials_one_holdout_confirmation` on `(strategy_id) WHERE
run_type = 'holdout_confirmation'`, enforcing the one-shot seal at the DB
level (not just in `TrialRecorder` application code) so a second holdout
run cannot slip through a future bypass of the recorder. The predicate keys
on `run_type` alone, **not** `AND status='completed'`: because
`TrialRecorder` (§4.4) inserts the trial row *before* dispatch and the run
reads the sealed holdout data *during* dispatch, a holdout attempt that
reads the data and then errors has already consumed its single permitted
look — so the seal must trip on the first attempt, exactly matching §4.2's
"no prior `holdout_confirmation` trial row exists (any status)". A
completed-only predicate would leave the failure/retry path able to re-read
the sealed data. Accepted fail-closed tradeoff: an errored holdout attempt
permanently consumes the seal; re-running requires an operator append-only
audit correction (C3), never a silent retry.

**`research_data_windows`** — the train/OOS/holdout partition itself.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `strategy_family` | `TEXT` | Nullable; when set, applies to every `strategy_id` sharing this family (mirrors `strategies.strategy_family`) — see open question 1 |
| `strategy_id` | `TEXT` | Nullable; a per-strategy override when a family-level window does not apply |
| `train_start` / `train_end` | `DATE NOT NULL` | |
| `oos_start` / `oos_end` | `DATE NOT NULL` | |
| `holdout_start` / `holdout_end` | `DATE NOT NULL` | |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

**Constraint:** `ck_research_data_windows_scope`: exactly one of
`strategy_family`/`strategy_id` is non-null (`CHECK ((strategy_family IS
NULL) != (strategy_id IS NULL))`). **Constraint:**
`ck_research_data_windows_order`: `train_start < train_end AND train_end <=
oos_start AND oos_start < oos_end AND oos_end <= holdout_start AND
holdout_start < holdout_end` — the three windows are mechanically
non-overlapping and chronologically ordered at the DB level, not merely by
convention.

**`promotion_decisions`** — one row per `PromotionPipeline.run` invocation;
append-only audit record cited by the `backtesting→validated` transition.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `strategy_id` | `TEXT NOT NULL` | |
| `config_hash` | `TEXT NOT NULL` | FK → `strategy_definitions(strategy_id, config_hash)` |
| `n_trials_used` | `INTEGER NOT NULL` | The `strategy_trials` count actually passed into `deflated_sharpe_ratio` — persisted so a reviewer never has to trust an unlogged in-memory query |
| `dsr_value` | `NUMERIC` | |
| `funnel_passed` | `BOOLEAN NOT NULL` | |
| `sensitivity_verdict` | `TEXT` | `robust` \| `curve_fit` |
| `stress_verdict` | `TEXT` | `solid` \| `fragile` |
| `overall_passed` | `BOOLEAN NOT NULL` | |
| `mlflow_run_id` | `TEXT` | |
| `evidence_json` | `JSONB NOT NULL` | Full structured evidence bundle (all gate detail, all stage outputs) |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

### 5.2 Migration `015_strategy_registry_validated_status.py`

Separate migration (kept distinct from `014` so a reviewer can evaluate the
lifecycle-altering, higher-blast-radius change to an existing table
independently from the purely additive `014`):

- Alter `strategies.status` CHECK constraint from `IN ('backtesting',
  'paper', 'live', 'archived')` to `IN ('backtesting', 'validated', 'paper',
  'live', 'archived')`.
- Alter `strategy_status_history.to_status` CHECK constraint identically.
- No data migration needed (no existing row can hold the new value yet);
  this is a pure constraint widening, reviewed the same way 03A-4a's
  migration 013 was (additive, no existing-row rewrite).

## 6. Integration points

- **Strategy Registry lifecycle:** §4.5's `validated` status and the
  `promotion_result_ref`-gated `backtesting→validated` transition are the
  primary integration point. `strategy_registry/registry.py`'s
  `transition()` method and its transition-table dict both need the new
  edge; `strategy_registry/models.py`'s two CHECK constraints need the
  migration 015 change mirrored in the ORM model declarations (the existing
  pattern already keeps Alembic migrations and `models.py` CheckConstraint
  strings in sync manually — e.g. `data/research/models.py`'s docstring
  calls out mirroring `012_research_identity.py` explicitly; this plan
  follows the same discipline).
- **`BacktestLogger`/MLflow (C7):** `PromotionPipeline` calls
  `log_walk_forward_run(..., funnel_result=..., stress_result=...,
  require_manifest_data_version=True)` for the walk-forward/funnel/stress
  legs (all three parameters already exist — no `mlflow_logger.py` signature
  change needed there). A small **new** addition is needed for DSR/FDR:
  neither `log_run` nor `log_walk_forward_run` currently logs anything
  DSR/FDR-shaped. §7's 04-4 slice adds an optional `overfitting_result:
  dict` parameter (or a dedicated `log_promotion_decision(...)` method,
  TBD at implementation time per open question 4) that logs `dsr.value`,
  `dsr.n_trials`, and `dsr.n_observations` as MLflow metrics/tags — additive
  only, no existing signature changes.
- **Config versioning (C6):** §4.3's drift check reuses
  `StrategyRegistry.verify_config_integrity()` unchanged. Per C6, a config
  that has already reached `validated`/`paper`/`live` must never be edited
  in place; a revised strategy after promotion is a new `v{N+1}_...yaml`
  and, per the two-level identity model, a genuinely new `strategy_id` or at
  minimum a new `config_hash` requiring a fresh trip through this entire
  protocol (no shortcut from an old promotion decision to a tweaked config).
- **Simulation clock discipline:** `TrialRecorder`, `PromotionPipeline`, and
  the new migrations introduce no wall-clock reads inside any backtest
  execution path — `started_at`/`completed_at`/`created_at` columns are
  operator-facing audit timestamps (when the *governance action* happened),
  analogous to `strategy_runs.started_at`/`ResearchRun.created_at`, not
  simulation-time values. No `datetime.now()` call is introduced inside
  `backtesting/engine/` or any code path `BacktestEngine`/`WalkForwardValidator`
  consult for simulated dates — the sole clock uses are for governance
  bookkeeping, matching the existing precedent of `strategy_runs.started_at`.
- **Airflow/paper pipeline:** no DAG changes. The DAG's existing
  `registry.list(status='paper')` lookup (per the Strategy Registry spec
  §8) is unaffected; a strategy must pass through `validated` before it can
  ever reach `paper`, but the DAG only ever looks for `paper`-status
  strategies, so no DAG code needs to know about the new intermediate state.

## 7. Phased implementation breakdown

| Slice | Deliverable | Difficulty | Depends on | Files touched | Acceptance evidence |
|---|---|---|---|---|---|
| **04-1** | Migration `014`: `strategy_hypotheses`, `strategy_trials`, `research_data_windows`, `promotion_decisions` tables + ORM models mirroring them (new `research/selection_models.py` or similar, following the `data/research/models.py` pattern of a standalone module mirroring the migration). No behavior change yet — pure schema. | M | None (additive only; can start immediately) | `infra/db/migrations/versions/014_strategy_selection_protocol.py`, new ORM module, `tests/` schema tests | All four tables/constraints exist; a hand-written test proves the window-ordering CHECK rejects an overlapping/misordered window and the one-holdout-confirmation partial unique index rejects a second completed holdout row. |
| **04-2** | `TrialRecorder` (`backtesting/validation/trial_recorder.py`): wraps `WalkForwardValidator.run`/`ParameterSweeper.sweep`, inserts a `strategy_trials` row before dispatch, updates it after, enforces the §4.2 holdout-window guard (`HoldoutWindowViolationError`) and the one-shot seal. | L | 04-1 (needs the tables) | `backtesting/validation/trial_recorder.py`, tests mirroring `backtesting/tests/test_walk_forward.py`/`test_parameter_sensitivity.py` patterns | A wrapped walk-forward run whose date range overlaps a registered holdout window is rejected without executing the engine; a wrapped run inside train/OOS succeeds and leaves exactly one `strategy_trials` row with correct `window`; a crashed wrapped run still leaves a `status='errored'` row (proving Gap 1's "can't just not report the bad ones" closure); a second `holdout_confirmation`-mode call for the same `strategy_id` after a completed one is rejected. |
| **04-3** | `strategy_hypotheses` write path + CLI/API to pre-register a hypothesis and its frozen `param_grid_json` before any trial runs; `frozen_at` enforcement in `TrialRecorder`. | S | 04-1, 04-2 | `strategy_registry/cli.py` or a new `research/hypothesis_cli.py`, tests | A `param_grid_json` write attempt after `frozen_at` is set (i.e., after the first linked trial) is rejected; a trial linked to a hypothesis whose `frozen_at` is still null freezes it as a side effect. |
| **04-4** | `PromotionPipeline` (`backtesting/validation/promotion_pipeline.py`): orchestrates funnel → sensitivity → stress → DSR/FDR using `TrialRecorder`-sourced `n_trials`; writes a `promotion_decisions` row; small additive `mlflow_logger.py` change to log DSR/FDR fields. | L | 04-2, 04-3 | `backtesting/validation/promotion_pipeline.py`, `backtesting/experiment_tracking/mlflow_logger.py` (additive param only), tests | Running the pipeline against a fixture strategy with a known-good walk-forward produces a `promotion_decisions` row whose `n_trials_used` matches an independently computed `COUNT(*)` from `strategy_trials`; a fixture strategy that fails any one stage produces `overall_passed=False` with the correct stage attributed in `evidence_json`; DSR/FDR values appear as MLflow tags on the resulting run. |
| **04-5** | Migration `015`: add `validated` status; `strategy_registry/registry.py` transition-table update requiring a valid `promotion_result_ref` on the `backtesting→validated` edge only; `models.py` CHECK-constraint mirror update. | M | 04-4 (needs `promotion_decisions` rows to reference) | `infra/db/migrations/versions/015_strategy_registry_validated_status.py`, `strategy_registry/models.py`, `strategy_registry/registry.py`, `strategy_registry/tests/` | A `transition(to_status='validated')` call without a passing `promotion_result_ref` raises a new `MissingPromotionEvidenceError`; one with a valid passing reference succeeds and appends the correct `strategy_status_history` row; `validated→paper` continues to work exactly as `backtesting→paper` did before (existing paper-readiness preflight untouched); `archived` remains terminal from `validated` too. |
| **04-6** | End-to-end integration test + a runnable example script (`scripts/run_promotion_pipeline.py`, read-only-safe, mirrors the existing `scripts/paper_*_check.py` style of an operator-runnable, narrowly-scoped command) demonstrating the full flow in §4.0 against a fixture strategy from hypothesis registration through a `validated` transition, using a small synthetic holdout confirmation. | M | 04-1..04-5 | `scripts/run_promotion_pipeline.py`, `tests/integration/test_promotion_protocol_e2e.py` | The fixture strategy reaches `validated` status only after all nine steps in §4.0 execute in order; attempting to skip step 8 (holdout confirmation) and go straight to a `paper` transition is rejected by the existing (unmodified) Strategy Registry transition rules, since `paper` is only reachable from `validated`, and `validated` itself required a passing `promotion_decisions` row — proving the whole chain is enforced end-to-end, not just each link in isolation. |

Suggested sequencing: 04-1 is the unblocking item and should run first, alone.
04-2 and 04-3 can run in parallel once 04-1 merges (disjoint files: the
recorder module vs. the hypothesis CLI), though 04-3's `frozen_at`
enforcement technically depends on 04-2's insert path existing, so treat
04-3 as starting slightly behind 04-2 rather than truly parallel. 04-4 must
wait for both. 04-5 touches the same `strategy_registry/` files several
other in-flight or recently-merged builder tracks have touched (per the
Roadmap's delivery log, `strategy_registry/` was last modified by M5.1); it
should be sequenced to avoid colliding with any other concurrently open PR
against that directory. 04-6 is last and is intentionally the smallest
"prove it end-to-end" slice, kept separate from 04-5 so a reviewer can
evaluate the lifecycle change and the integration proof independently.

## 8. Open questions for operator sign-off

1. **Window scope — per-strategy or per-family?** §5.1's
   `research_data_windows` allows either `strategy_family` or `strategy_id`
   scoping. Is the intent one shared train/OOS/holdout calendar boundary for
   all strategies in a family (e.g., all momentum variants share the same
   holdout dates, which is stronger protection against multiple-testing
   across the family) or does each `strategy_id` get its own boundary? The
   family-level default is recommended (it is the only choice that actually
   bounds family-wide multiple-testing), but it constrains how future
   strategy families can be dated relative to each other and needs explicit
   sign-off.

   **RESOLVED (2026-07-22):** PER-STRATEGY is the enforced default. The
   schema still supports per-family windows via the nullable
   `strategy_family`/`strategy_id` columns on `research_data_windows` (the
   scope-XOR CHECK constraint, §5.1), but per-strategy is the norm going
   forward. This is intentionally mixed with Q6's per-family FDR scope
   resolution below: window sealing is per-strategy, but the multiple-
   testing correction that reads those windows' trial outcomes is
   family-wise.
2. **Holdout length and exact boundary dates.** This plan defines the
   *mechanism* (a sealed, one-shot window) but not the specific calendar
   dates or duration. Given the currently supported backtest window
   (`2022-07-11` through `2024-12-31` per `CLAUDE.md`), what fraction should
   be reserved as final holdout, and is that even enough calendar span to
   support a statistically meaningful holdout confirmation on top of
   sufficient train/OOS folds? This may reveal the currently pinned dataset
   is too short for a three-way split to be meaningful, in which case
   extending the ingested price history (noted in `CLAUDE.md` as a known
   gap requiring ingestion back to ~2018) may be a prerequisite the operator
   needs to schedule before 04-6 can run against real data rather than a
   synthetic fixture.

   **RESOLVED (2026-07-22):** Build the protocol now against synthetic
   fixtures. The ~2018+ price-history backfill needed for a real
   train/OOS/holdout split on real data is a scheduled prerequisite before
   any REAL strategy is qualified through this protocol, but it does NOT
   block this schema work (04-1) or the recorder/pipeline slices
   (04-2..04-6), which are proven against fixtures first.
3. **Promotion thresholds.** Should `validated` require *all six* survival
   funnel gates plus `robust` sensitivity plus `solid` stress plus a DSR
   above some explicit numeric floor (e.g., DSR > 0.95), or is DSR reported
   as evidence without its own hard pass/fail gate at this stage? The design
   as written treats DSR as informational in `promotion_decisions.dsr_value`
   but does not currently gate `overall_passed` on it — confirm whether that
   is the intended strictness or whether a DSR floor should be a seventh
   hard gate.

   **RESOLVED (2026-07-22):** INFORMATIONAL ONLY. `dsr_value` is recorded
   in `promotion_decisions`/evidence for every promotion, but `overall_passed`
   is NOT gated on any DSR numeric floor. No seventh hard gate is added.
4. **Mandatory-blocking or advisory trial recording?** §4.1/§4.2 make
   `TrialRecorder` a hard gate for anything reaching `promotion_decisions`
   — a direct, unwrapped `WalkForwardValidator.run` call is structurally
   incapable of updating `strategy_trials`, so a hypothetical bypass produces
   no promotion evidence rather than bad evidence. Confirm this "the recorder
   is the only door" design is acceptable versus a softer advisory mode
   where unwrapped runs are still permitted for quick exploratory iteration
   but flagged/excluded from `n_trials` rather than blocked outright. The
   plan's default is closer to "advisory for exploration, blocking for
   anything that will ever be cited in a promotion decision" — but that
   means a builder could explore off-protocol and then have to redo the
   winning run through the recorder to make it promotion-eligible; confirm
   that friction is intentional rather than something to soften further.

   **RESOLVED (2026-07-22):** HYBRID. Unwrapped, direct instrument calls
   remain permitted for quick exploratory iteration (advisory mode) and are
   simply excluded from `n_trials`/promotion evidence. `TrialRecorder`
   becomes a hard block only for anything that will ever be cited in a
   `promotion_decisions` row -- confirmed, the friction of redoing a winning
   run through the recorder before it becomes promotion-eligible is
   intentional.
5. **Selection automation.** §4.0 step 4 treats the winning-config choice as
   a human decision. Should any part of selection (e.g., auto-selecting the
   single highest-DSR candidate among those that already passed
   `SurvivalFunnel`) ever be automated, or must a human always pick which
   candidate proceeds to promotion? Given C1/C8's precedent of never letting
   automation make an unreviewed consequential decision, human selection is
   assumed as the default in this plan; confirm.

   **RESOLVED (2026-07-22):** HUMAN-ONLY. Automation may rank/recommend
   candidates (e.g. surfacing the highest-DSR candidate among those that
   already passed `SurvivalFunnel`), but selection itself must always be an
   explicit human decision, consistent with the C1/C8 precedent.
6. **Cross-strategy FDR scope.** `benjamini_hochberg` operates over a
   *set* of p-values. Is the intended family-wise comparison "every trial
   ever run for this one `strategy_id`" (narrow), "every trial run for every
   strategy in the same `strategy_family`" (medium), or "every trial ever
   run against any strategy in the registry" (broad, the textbook-correct
   scope for controlling repository-wide false discovery, but operationally
   heavier and requiring every historical trial to have been captured by the
   new registry from day one)? This materially changes how conservative
   promotion becomes as the strategy library (M5.6, 21 composite signals
   across 8 groups) grows.

   **RESOLVED (2026-07-22):** PER-FAMILY. Benjamini-Hochberg is applied
   across sibling-strategy trials within the same `strategy_family`, not the
   narrower single-`strategy_id` scope nor the broader registry-wide scope.
   Paired with Q1's per-strategy window resolution above: window sealing is
   per-strategy, the FDR correction that consumes those trials' outcomes is
   family-wise.
7. **Promotion-pipeline re-run staleness bound.** §4.4 allows reusing an
   already-recorded walk-forward result "within a configurable staleness
   bound" rather than always re-running. What should that bound be (e.g.,
   reuse only if computed against the exact same `data_version` and less
   than N days old), or should the pipeline always re-run everything fresh
   at promotion time regardless of cost, given C7's data-version pinning
   already guarantees byte-identical inputs, making staleness a pure
   compute-cost question rather than a correctness one?

   **RESOLVED (2026-07-22):** Re-run fresh at promotion time, UNLESS the
   already-recorded run used the identical manifest-hash `data_version` --
   in that case the recorded result may be reused. This makes staleness a
   pure compute-cost decision keyed strictly off `data_version` equality,
   never off elapsed wall-clock time.
8. **Residual-bug handling (BUG-066/068/071) at promotion time.** Should the
   promotion pipeline actively surface these residuals inline in the
   evidence bundle (e.g., a boolean tag "ran against a strategy config with
   no configured minimum-eligible-count gate," per BUG-066) so a reviewer
   sees the caveat every time, or is a one-time acknowledgment (this
   document, §9) sufficient until those bugs are separately fixed? Given
   this project's practice elsewhere (e.g. 03A-4b surfacing
   `provisional_no_known_at` labels inline rather than only in a design doc),
   inline surfacing is likely the more consistent choice, but it adds scope
   to 04-4 (reading and stamping bug-status flags into the evidence bundle)
   that the current phased breakdown does not include — confirm whether
   that belongs in 04-4 or is acceptable as a later follow-up slice.

   **RESOLVED (2026-07-22):** Surface BUG-066/068/071 INLINE in every
   promotion evidence bundle (`promotion_decisions.evidence_json`), not just
   as a one-time acknowledgment in this document. This belongs in 04-4's
   scope (reading and stamping the residual-bug flags into the evidence
   bundle when `PromotionPipeline.run` executes).

## 9. Acknowledged residual limitations this protocol operates on top of

This protocol makes strategy selection disciplined and auditable; it does
not repair the following pre-existing, separately-tracked research-validity
gaps, all still `Open` in `bugs.md` as of this writing:

- **BUG-066** (cross-sectional scoring has no minimum-eligible-count
  enforcement): a promoted strategy's underlying alpha scores could still
  have been computed from a silently shrunken cross-section on some dates.
  The survival funnel's `min_trade_count` gate provides partial, indirect
  protection (a strategy with pervasively shrunken cross-sections tends to
  trade thinly), but this is not a substitute for BUG-066's fix. Open
  question 8 asks whether this protocol should surface that gap inline.
- **BUG-068** (Wikipedia constituent history has bounded count drift,
  ~3% recent-era inflation, left-censored pre-2000 intervals, three
  excluded ticker-collision symbols): any walk-forward fold or holdout
  window drawing on the affected universe/date ranges inherits this
  drift. It biases toward mild over-inclusion, not the survivorship
  direction BUG-008 already fixed, so it is not expected to invalidate a
  promotion decision on its own, but a reviewer evaluating a
  `promotion_decisions` evidence bundle should know the underlying universe
  is not a licensed point-in-time feed yet.
- **BUG-071** (score-series cutoff-aware adjustment uses one run-boundary
  cutoff for the residual same-session-`ex_date` edge case, score leg only —
  the realized-return leg's more severe version of this issue is already
  fixed): affects the alpha scores a strategy's signal is built from, one
  narrow single-session edge case per affected ticker/action. Bounded in
  scope per the existing bug write-up; not expected to change any
  promotion verdict materially, but inherited unchanged by this protocol.

None of these three are blocking dependencies for Gate 04 per the Roadmap
(only 03A, 03B, and 02B are listed as Gate 04 dependencies, and all three
are `Delivered`); they are documented here so a future reviewer of a
`promotion_decisions` row does not mistake "cleared this protocol's gates"
for "immune from every known research-data caveat in the repository."

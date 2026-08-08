# RQIS Current Delivery Roadmap

**Current baseline:** `main` at `39c5e5b` (2026-06-30). This roadmap was rebuilt on 2026-07-12 from the synced repository, current `bugs.md`, `CLAUDE.md`, `Worklog.md`, and three independent reviews of execution, strategy/research, and dashboard/operator experience. Reviewed 2026-07-19 (post-Gate-01): BUG-070 added as row 03B, Gate 04 dependencies/citations extended, current decision updated to the Gate 02/03 front.

This is the task-selection plan, not a second bug tracker. `bugs.md` remains the detailed finding register; `PRD.md` remains product scope; `Worklog.md` remains the chronological record.

## How to use it

- **Recommended Order:** numbers are sequential gates. Lettered tasks within the same number may proceed in parallel when their dependencies permit it. Do not start a later number until its prior gate has met its stated acceptance evidence.
- **Status:** `Ready` means implement now; no separate specification-writing step is implied. `Delivered` means code or documentation exists, but does not by itself mean operationally qualified. `Blocked` means a listed dependency must finish first. `Deferred` requires a future go/no-go decision.
- **Spec Details:** links to the existing source that defines the problem or acceptance context. A future design document may replace the link only when a task needs deeper design.
- **Completed:** means the row's named deliverable exists. It does not assert that all downstream qualification, deployment, or live-readiness gates have passed.

## Delivery plan

| Completed | Issue | Priority | Recommended Order | Category | Difficulty | Status | Dependencies | Spec Details | Description | Date Created | Date Completed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Yes | Strategy registry and lifecycle foundation | P1 | Baseline | Strategy Governance | L | Delivered | None | [Strategy registry spec](docs/strategy_registry_spec.md) | DB-backed catalog, lifecycle history, canonical config identity, and run records are implemented; promotion evidence still needs hardening. | 2026-06-23 | 2026-06-23 |
| Yes | Append-only trade journal | P1 | Baseline | Execution Data | L | Delivered | None | [Trade journal history](Worklog.md) | Fill persistence, FIFO P&L, and wash-sale context are implemented; true broker-fill idempotency remains open. | 2026-06-23 | 2026-06-23 |
| Yes | Tearsheets with charts | P2 | Baseline | Reporting | L | Delivered | None | [Phase 5 milestones](CLAUDE.md) | Backtest reporting metrics, charts, HTML, and PNG output are implemented; paper/live evidence integration remains part of qualification. | 2026-06-24 | 2026-06-24 |
| Yes | Automated paper-trading DAG | P0 | Baseline | Execution | XL | Delivered | None | [DAG specification](docs/airflow_paper_dag_spec.md) | The 13-task Airflow workflow is implemented, but it is not yet deployment-smoked or operationally qualified. | 2026-06-25 | 2026-06-25 |
| Yes | Streamlit dashboard | P1 | Baseline | Operator UI | XL | Delivered | None | [Dashboard specification](docs/streamlit_dashboard_spec.md) | Seven dashboard pages, approval UI, risk, performance, signals, and audit views exist; they are not yet an authoritative control plane. | 2026-06-29 | 2026-06-29 |
| Yes | Signal library and validation toolkit | P1 | Baseline | Research | XL | Delivered | None | [Phase 5 milestones](CLAUDE.md) | Composite signals, registry support, parameter sensitivity, and survival-funnel tooling exist; strategy validity remains unproven. | 2026-06-28 | 2026-06-28 |
| Yes | Make Compose paper runtime executable | P0 | 01A — Runtime foundation | Platform | L | Delivered | None | [BUG-001 to BUG-004](bugs.md) | Pass all required paper/IBKR settings into Airflow, install DAG runtime dependencies, mount the shared artifact directory, and configure host-to-IBKR connectivity. | 2026-07-12 | 2026-07-18 |
| Yes | Repair research-validity baseline | P0 | 01B — Research foundation | Research | XL | Delivered | None | [BUG-008 to BUG-010](bugs.md) | Replace current-membership historical universes, define and enforce signal-to-trade timing, and remediate unsafe missing-data defaults across the indicator library. | 2026-07-12 | 2026-07-19 |
| No | Prove the Compose no-submit workflow | P0 | 02A — Runtime proof | Platform | L | Ready | Make Compose paper runtime executable | [DAG runbook](docs/runbooks/airflow_fire_drill.md) | Entry condition: operator first runs migration 009 (`alembic upgrade head`) and the documented PIT universe import against `DATABASE_URL` (pending from 01B-2). Then build the images, apply migrations, import both DAGs, verify shared artifacts and IBKR reachability, then complete a no-submit DAG run with retained evidence. | 2026-07-12 | |
| Yes | Fail closed on unsupported strategy configuration | P0 | 02B — Semantic proof | Strategy Correctness | L | Delivered | Repair research-validity baseline | [v2 strategy config](config/strategy/v2_mvo_momentum.yaml) | Reject-unsupported-fields path chosen (MVO/risk-parity backtester semantics deferred): shared fail-closed `validate_backtest_config` at all 6 backtest entry points + per-key conformance test. Merged as PR #36 (BUG-075). | 2026-07-12 | 2026-07-20 |
| Yes | Make research data immutable and PIT-complete | P0 | 03A — Reproducible research | Data / Research | XL | Delivered | Repair research-validity baseline | [BUG-037 to BUG-039](bugs.md) | Delivered via phased 03A-1..03A-5 (PRs #37/#38/#39/#41/#42/#44): content-addressed immutable snapshots + manifest integrity, fail-closed object-store taxonomy, same-date corporate-action fix, PIT eligibility schema + data population, and manifest/methodology linkage with `data_version` cutover. | 2026-07-12 | 2026-07-22 |
| Yes | Split backtester prices into execution and analytic series | P1 | 03B — Reproducible research | Backtesting | M | Delivered | Repair research-validity baseline | [BUG-070](bugs.md); [design plan §2](docs/plans/01b-research-validity-design.md) | Delivered via PR #40 (BUG-070). Raw tradable execution series (fills, cash, share accounting) + cutoff-aware analytic builders; fails closed on price-gap/missing corporate-action data. Follow-up BUG-079 (wire analytic series into reporting) remains open. | 2026-07-19 | 2026-07-20 |
| No | Establish a real strategy-selection protocol | P1 | 04 — Research qualification | Strategy Validation | XL | Ready | Make research data immutable and PIT-complete (done); Split backtester prices into execution and analytic series (done); Fail closed on unsupported strategy configuration (done) | [Backtesting validation](backtesting/validation); residual caveats [BUG-066, BUG-068, BUG-071](bugs.md) | All three dependencies delivered (2026-07-22) — now Ready. Record hypotheses and trials, select only inside training windows, freeze configuration, test out of sample, retain a final holdout, and capture all variants for multiple-testing analysis. | 2026-07-12 | |
| No | Persist one authoritative safety state | P0 | 05A — Shared control plane | Risk / Execution | XL | Blocked | Prove the Compose no-submit workflow | [BUG-007, BUG-012](bugs.md) | Persist circuit-breaker events, alerts, acknowledgements, qualification state, and operational exceptions; dashboard and Airflow must read the same state and fail closed when unavailable. | 2026-07-12 | |
| No | Secure and bind approval to the exact DAG run | P0 | 05B — Shared control plane | Security / Operator UI | XL | Blocked | Persist one authoritative safety state | [BUG-005, BUG-011 to BUG-016](bugs.md) | Authenticate approvers, enforce approval/reset roles, publish explicit approval requests per DAG run, bind the canonical artifact/hash/expiry, and reuse submission-side schema validation. | 2026-07-12 | |
| No | Handle indeterminate orders and duplicate fills | P0 | 06A — Recovery correctness | Execution | XL | Blocked | Secure and bind approval to the exact DAG run | [BUG-006, BUG-042 and BUG-048](bugs.md) | Persist broker correlation identity, classify post-placement timeout as indeterminate, require broker reconciliation before retry, and deduplicate fills by true execution identity or cumulative-fill semantics. | 2026-07-12 | |
| No | Complete risk and compliance enforcement | P0 | 06B — Recovery correctness | Risk / Compliance | L | Blocked | Persist one authoritative safety state | [BUG-041 and BUG-050](bugs.md) | Enforce sector concentration breaches, fail closed for insufficient/NaN risk data, and ensure real wash-sale, position, and sector context reaches the DAG pre-submit gate. | 2026-07-12 | |
| No | Build the operator exception and lineage workflow | P1 | 07A — Operator readiness | Operator UI | XL | Blocked | Handle indeterminate orders and duplicate fills; Complete risk and compliance enforcement | [Dashboard specification](docs/streamlit_dashboard_spec.md) | Show run state from approval through reconciliation, an owned exception queue, full approval overrides, broker outcomes, lineage, freshness, and explicit `empty` versus `error` states. | 2026-07-12 | |
| No | Correct dashboard freshness and status semantics | P1 | 07B — Operator readiness | Operator UI | L | Blocked | Persist one authoritative safety state | [Dashboard query layer](reporting/dashboards/queries.py) | Make every safety-relevant panel strategy/run-specific; show source, observation time, producer time, SLA, age, and clear unavailable/misconfigured/error states. | 2026-07-12 | |
| No | Align operational documents and deployment contracts | P1 | 07C — Operator readiness | Documentation / Platform | M | Blocked | Prove the Compose no-submit workflow; Persist one authoritative safety state | [CLAUDE.md](CLAUDE.md) | Update dashboard/DAG specs, runbooks, environment names, and milestone wording to match actual producers, safety gates, and deployment behavior. | 2026-07-12 | |
| No | Prove dashboard and DAG runtime behavior | P0 | 08 — Runtime qualification | Verification | XL | Blocked | Build the operator exception and lineage workflow; Correct dashboard freshness and status semantics; Align operational documents and deployment contracts | [Dashboard test requirements](docs/streamlit_dashboard_spec.md) | Add Streamlit/browser/container smoke coverage and run human drills for DB outage, malformed/stale artifacts, concurrent approvals, restart recovery, risk trip/reset, and shared-volume behavior. | 2026-07-12 | |
| No | Calibrate research against production reality | P1 | 09 — Model qualification | Strategy Validation | XL | Blocked | Establish a real strategy-selection protocol; Handle indeterminate orders and duplicate fills | [Fill simulator](backtesting/engine/fill_simulator.py) | Establish research-to-paper target/order parity, supply real ADV/spread/volatility inputs, enforce participation limits, run capacity stress, and compare simulated costs with paper fills. | 2026-07-12 | |
| No | Derive qualification from durable evidence | P0 | 10 — Qualification gate | Governance | L | Blocked | Prove dashboard and DAG runtime behavior; Calibrate research against production reality | [C8 rule](CLAUDE.md) | Replace manual clearance flags with a durable record of qualifying runs, scenario evidence, reconciliations, incidents, and explicit human signoff. | 2026-07-12 | |
| No | Run four-week automated paper qualification | P0 | 11 — Qualification gate | Operations | XL | Blocked | Derive qualification from durable evidence | [Phase 5 exit criterion](PRD.md) | Complete four clean automated paper weeks using the actual DAG and operator surface; retain evidence for every approval, exception, reconciliation, alert, and recovery drill. | 2026-07-12 | |
| No | Independent live-readiness review | P1 | 12 — Live decision | Security / Governance | XL | Blocked | Run four-week automated paper qualification | [Live-trading safety rules](CLAUDE.md) | Review security, deployment, approval, recovery, risk limits, monitoring, strategy evidence, and qualification records before any live-capital approval. | 2026-07-12 | |
| No | Controlled small-capital launch | P3 | 13 — Live decision | Live Trading | XL | Deferred | Independent live-readiness review | [Phase 5 scope](PRD.md) | Requires explicit operator approval, hard capital and loss limits, attended operation, rollback criteria, and a post-launch stability review. | 2026-07-12 | |

## Current decision

**Updated 2026-07-22 (PM session).** Gates 01, 02B, and all of Gate 03 (03A-1
through 03A-5 + 03B) are delivered and merged into `dev/R2-phase1`. No open PRs.
The research-reproducibility foundation (immutable PIT snapshots, split
execution/analytic prices, config fail-closed) is complete, so **Gate 04
(strategy-selection protocol) is now unblocked and is the active builder front.**
Gate 04 is being run design-doc-first (mirroring the successful 01B/03A pattern):
a Phase-0 protocol spec for PM+operator review before any implementation.

**Two parallel tracks from here:**

1. **Research track (builder-actionable now):** Gate 04 — establish the
   disciplined strategy-selection protocol (hypothesis/trial registry,
   train/OOS/holdout governance, config freeze, multiple-testing correction).
   Independent of the platform/execution track; touches `backtesting/` + docs.
2. **Platform/execution track (OPERATOR-GATED — blocked, cannot be done by
   agents):** Gate 02A (no-submit Compose proof) requires the operator to run
   `alembic upgrade head` (migrations **009 and 013**), the documented PIT
   universe import against `DATABASE_URL`, then a live Compose/TWS no-submit DAG
   run with retained evidence. **02A gates the entire 05→08 control-plane and
   execution-correctness track.** Until the operator clears 02A, Gates 05A/05B/
   06A/06B/07*/08 stay blocked. This is the critical-path bottleneck and only the
   operator can clear it.

Continue to treat current backtest results as non-evidence for strategy
selection until Gate 04 establishes the protocol. Do not begin the four-week
automated paper clock.

## Delivery execution log (R2 round)

Managed by the project-manager session. Integration branch: `dev/R2-phase1`.
Task branches are named `dev/R2-<order>-<slug>`. Each phased slice is one
commit; each roadmap job (or sub-job below) is one PR into `dev/R2-phase1`.
Token figures are reported as `<agent/effort>: <tokens>`.

**PM decisions (2026-08-08, PR #49 split — 04-4 scope recovery):**

- **PR #49 is split into three PRs.** The slice grew from 5 files/~3.5k lines to
  22 files/5,709 lines and absorbed a root-of-trust fingerprint-algorithm change,
  two migrations, and a new evaluator — none of which is 04-4. It ran **~12 Codex
  review rounds**, blowing the mandated cap (stop at two consecutive P0/P1-clean
  rounds, max 4). Scope, not code quality, kept the loop alive.
  - **04-4A** (`dev/R2-04-4-promotion-pipeline`, PR [#49](https://github.com/gumfactor/Agentic-Investing/pull/49)
    rewound to `47d6b65`): the train/OOS promotion path with `holdout_mode`
    gated fail-closed. Verified green at **515 passed**. Ready for operator merge.
  - **04-4W** (`dev/R2-04-4W-evaluation-window`): evaluation window as a
    first-class measurement input.
  - **04-4H** (`dev/R2-04-4H-holdout-confirmation`): holdout confirmation with a
    look-triggered seal. **Depends on 04-4W — runs sequentially, not in parallel**
    (both touch `trial_recorder.py`/`promotion_pipeline.py`).
  - All original commits preserved on `backup/pr49-full-756aea9` and
    `backup/pr49-core-47d6b65`. Nothing discarded.
- **Two recurring P1 classes diagnosed; both to be closed by general rule, not
  instance patches** — the same playbook that closed 04-2 (5 rounds, one class,
  fixed with the general dot-path-ancestry rule) and 03A-4b (4 instances, closed
  by PM class sweep):
  - **Class A — the one-shot holdout seal is consumed by *intent to look*, not by
    an actual look.** `TrialRecorder` INSERTs the seal row before dispatch, and
    `uix_strategy_trials_one_holdout_confirmation` keys on `run_type` at ANY
    status, so *any* exception between INSERT and the first price read burns an
    irreplaceable asset. That is an open set, and rounds R3-A/R4-A/R5 each closed
    one member (R5 was a regression introduced by the R4 fix). **General fix:**
    a `holdout_look_taken` tripwire flipped on the first holdout bar read; the
    seal index keys on that flag. Pre-look failures from *any* unenumerated cause
    no longer burn the seal. **Constraint: this must NOT regress 04-1 round 1**,
    which deliberately keyed the seal on any status to close the errored-holdout
    re-read hole — a run that reads holdout and *then* errors must still burn it.
  - **Class B — the evaluation window was removed from `config_hash` identity but
    never promoted to a first-class input.** It still lives inside the stored
    config dict while being non-identity, authoritative for what runs, and
    unrecorded on measurements; every consumer that reads it or fails to persist
    it is a bug (registry reuse, `StrategyTrial`, `StrategyRun`, …). **General
    fix:** an `EvaluationWindow` value object, required on every measurement API
    and injected at dispatch (as `data_version` already is), never read from the
    stored definition.
- **The waived back-compat finding gets encoded in code, not just docs.** Codex
  re-raised the fingerprint migration P1 because the operator's waiver lives only
  in a design doc. A persisted `FINGERPRINT_ALGO_VERSION` makes the finding moot
  rather than declined, and makes a future migration trivial if this project ever
  approaches live capital (C8).

**PM decisions (2026-07-22, Gate 04 kickoff):**

- **Gate 04 unblocked and started design-doc-first.** Deps 02B/03A/03B all
  merged. Phase-0 design plan delivered on branch
  `dev/R2-04-strategy-selection-protocol`
  ([docs/plans/04-strategy-selection-protocol-design.md](docs/plans/04-strategy-selection-protocol-design.md));
  PM-approved. Implementation phased 04-1..04-6 (schema → TrialRecorder →
  hypothesis registry → PromotionPipeline → `validated` status → e2e proof).
  The design doc rides with the first implementation PR (04-1), no standalone PR.
- **Operator answers to the design doc's §8 open questions (locked 2026-07-22):**
  - **Q2 dataset/holdout:** build the protocol now against synthetic fixtures;
    schedule the known ~2018+ price-history backfill as a prerequisite before
    any REAL strategy is qualified (protocol machinery is independent of dataset
    length). **New roadmap prerequisite implied — price-history ingestion back to
    ~2018 must precede real Gate-04 qualification / Gate 09.**
  - **Q1 window scope:** **per-strategy** train/OOS/holdout boundaries (schema
    still supports per-family; per-strategy is the enforced default).
  - **Q3 DSR:** **informational only** — record DSR in the evidence bundle, do
    NOT gate `overall_passed` on a DSR floor.
  - **Q6 FDR scope:** **per-family** Benjamini-Hochberg across sibling-strategy
    trials.
  - PM-adopted defaults (operator may override): Q4 recorder = hybrid
    (advisory for exploration, hard-block for promotion-cited runs); Q5 =
    human-only selection (C1/C8); Q7 = fresh re-run unless identical manifest-hash
    `data_version`; Q8 = surface BUG-066/068/071 inline in every evidence bundle.
- **02A remains the operator-only critical-path bottleneck** for the entire
  05→08 control-plane/execution track (migrations 009+013, PIT universe import,
  live no-submit Compose run). Gate 04 runs in parallel and does not depend on it.

**PM decisions (2026-07-20, wave 2 cont.):**

- **Codex review gate waived under rate limits (operator):** Codex hit its
  usage limits mid-round and returned only limit messages (no review). Operator
  authorized proceeding on internal adversarial review + PM certification for
  PRs with strong internal coverage (full REJECT→fix→re-review cycle), rather
  than stalling. PM still blocks merge on any internal-review P0/P1. Applies
  while Codex capacity is unavailable.
- **Hostile third-pass review is now standard for integrity-critical slices:**
  an operator-requested hostile review of 03A-1 (PR #38) found a P0
  (`load_manifest` did no integrity verification — the manifest root-of-trust,
  whose hash is the C7 `data_version`, was never re-hashed on load) plus a P1
  hash-collision (non-injective canonical row encoding) that the prior two
  reviews missed. Content-addressing/hashing/fail-closed slices get a hostile,
  execute-the-attack review before merge.

**PM decisions (2026-07-19, wave 2):**

- **02B and 03A launched in parallel** (02B code build; 03A Phase 0 design doc
  only, disjoint files). **03B is queued behind 02B's merge** — both touch
  `backtesting/` and `bugs.md`.
- **02B scope decision:** the slice builds one shared consumed-field contract +
  fail-closed validator for the backtest path and a per-key conformance test;
  it does NOT implement MVO/risk-parity in the backtester (would balloon L to
  XL and collide with 03B loader work).
- **03A is phased per its design plan**
  ([docs/plans/03a-immutable-research-data-design.md](docs/plans/03a-immutable-research-data-design.md),
  branch `dev/R2-03A-immutable-research-data`): 03A-1 (L, content-addressed
  snapshots), 03A-2 (M, fail-closed object-store taxonomy, starts from 03A-1),
  03A-3 (S, BUG-037 same-date multi-action fix, parallel-safe), 03A-4 (XL, PIT
  eligibility attributes), 03A-5 (M, manifest/methodology linkage +
  `data_version` cutover, last). The design doc gets no standalone PR — it
  merges with the first 03A implementation PR. Implementation phases are
  gated on operator answers to the doc's §6 open questions
  (shares_outstanding `known_at` source, ADV definition, security_type
  curation, legacy snapshot retention, MinIO WORM follow-up,
  `allow_missing_corporate_actions` blast radius).

**PM decisions (2026-07-16):**

- **01B is delivered as three PRs**, in this order, because a single XL
  research-validity PR would be unreviewable: **01B-1** missing-data return
  policy (BUG-010, plan §3), **01B-2** point-in-time universe contract
  (BUG-008, plan §1), **01B-3** signal-timing/corporate-action contract plus
  versioned research identity (BUG-009, plan §§2, 4). The design plan at
  [docs/plans/01b-research-validity-design.md](docs/plans/01b-research-validity-design.md)
  remains the single spec.
- BUG-005, BUG-006, and BUG-007 are now explicitly cited by rows 05B, 06A, and
  05A respectively so no critical trading-safety finding is outside the plan.
- 01A runtime evidence that requires a live TWS/IB Gateway session (broker
  preflight, readiness check from container context) is delivered as an
  operator-runnable checklist plus fail-closed tests; the operator executes the
  live steps before 01A is marked delivered.

| Order | Branch | PR | Status | Builder Tokens | PM Tokens |
|---|---|---|---|---|---|
| 04-4W (evaluation window) | `dev/R2-04-4W-evaluation-window` | [#50](https://github.com/gumfactor/Agentic-Investing/pull/50) | **In PR review (2026-08-08)** — second of three PRs from the #49 split. Closes the **evaluation-window class**: the window became a required, explicit parameter of every measurement API, injected into the dispatched config copy exactly as `data_version` is, and persisted on `strategy_trials` (016), `strategy_runs` (017), and `promotion_decisions` (018). `EvaluationWindow` value object; `NonCanonicalConfigHashError` binds promotion to the registered definition. **`cb9b4f3`'s `EvaluationWindowConflictError` reuse guard was deliberately DELETED, not ported** — once the window is never sourced from the stored definition, "same identity, different window" is legal by design; adversarial review reproduced the original attack and confirmed no surviving path lets stored dates drive execution. `FINGERPRINT_ALGO_VERSION=2` persisted (015) and **load-bearing on the read path** (`FingerprintAlgorithmVersionError` fires before any hash-equality diagnosis), making the repeatedly-raised back-compat P1 moot rather than declined — no data migration, operator waiver stands. **Class-closing invariant test** (`test_eval_window_invariant.py`) AST-discovers every `DeclarativeBase` in the repo and requires every model carrying `data_version` OR `config_hash` to either persist the window or sit in a justified allowlist. Review path: PM amendments A1/A2/A3 (version column was write-only; migration chain contradicted its filenames; **column default mislabelled every new row as v1**, manufacturing the condition it diagnoses) → adversarial APPROVE-WITH-FIXES (P1 invariant scoped to 1 of 3 Bases with a live counterexample; P2 SQLite `alter_column`; P3 reuse contract untested) → Codex R1 (P1 `promotion_decisions` had no window — a live instance the `data_version`-only predicate structurally could not see, fixed by widening to `config_hash`; P1 underpowered sensitivity sweep; P2 SQLite `ADD COLUMN DEFAULT` persists — PM's dialect-skip reasoning was wrong; nit stale docstrings). BUG-083 + BUG-084 filed rather than absorbed. Full suite **557 passed** (from 515). | Sonnet 5 medium: 587K (builder, cumulative incl. 3 amendment + fix rounds) + 137K (adversarial) | Opus 5: ~200K est. (split diagnosis, 5 independent verification runs, A1–A3 + F1–F3 + R1 finding rounds) |
| 04-4A (PromotionPipeline, train/OOS) | `dev/R2-04-4-promotion-pipeline` | [#49](https://github.com/gumfactor/Agentic-Investing/pull/49) | **Merged 2026-08-08** (`a5b7e1c`). **Rewound from `756aea9` to `47d6b65` by PM and rescoped**: the slice had grown from 5 files to 22 (5,709 lines), absorbing a root-of-trust fingerprint change, two migrations and a new evaluator, and ran **~12 Codex rounds against the mandated cap of 4** — scope creep, not code quality, kept the loop alive. Split into 04-4A / 04-4W / 04-4H; all original commits preserved on `backup/pr49-full-756aea9`. Two recurring P1 *classes* were diagnosed and assigned general-rule fixes rather than instance patches (the 04-2 / 03A-4b playbook): **seal-consumed-by-intent-to-look** → 04-4H, **window-not-first-class** → 04-4W. Residual: R1-B (underpowered sensitivity sweep) shipped in this merge and is fixed in #50 — see BUG-084. Original scope description follows — `backtesting/validation/promotion_pipeline.py`: orchestrates walk-forward → survival funnel → parameter sensitivity (grid from the linked hypothesis's frozen `param_grid_json`, fail-closed if absent) → bootstrap stress → Deflated Sharpe + per-family FDR; writes an authoritative `promotion_decisions` row; additive `BacktestLogger.log_promotion_decision`. `overall_passed` = funnel + `robust` + `solid`; DSR/FDR informational (§8 Q3). `n_trials` = Σ(configs_tested over train_oos sweep rows) + 1/walk_forward row. BUG-066/068/071 stamped into `evidence_json`. PM review + adversarial (APPROVE-WITH-FIXES, no P0) then **4 Codex rounds** (R1 NaN→JSONB P1; R2 2×P2 DSR-source/sweep-count; R3 MLflow-config P1 + single-trial-DSR; R4 holdout-provenance P1 + 2×P2), all fixed. **`holdout_mode` gated fail-closed** (`HoldoutConfirmationNotSupportedError`): the R4 P1 is a genuine identity-design gap — `config_hash` conflates strategy identity with the evaluation window, so the frozen winner can't be re-evaluated over the sealed holdout. Deferred to a dedicated slice pending operator sign-off on [docs/plans/04-identity-evaluation-context-design.md](docs/plans/04-identity-evaluation-context-design.md). **The train/OOS promotion path is complete and correct and ships now.** Full suite: **515 passed**. | Sonnet 5 medium: 145K (builder) + 118K (adversarial) + ~350K (5 fix rounds incl. holdout gating) | Opus 4.8: heavy (4-round babysit, wider-lens synthesis, design note) |
| 04-3 (hypothesis write-path) | `dev/R2-04-3-hypothesis-writepath` | [#48](https://github.com/gumfactor/Agentic-Investing/pull/48) | **Merged 2026-08-07** (`7618c5c`) — hypothesis pre-registration write path + CLI (`strategy_registry/hypothesis.py`), `param_grid_json` immutability enforcement, and the `frozen_at` freeze-on-first-linked-trial side effect in `TrialRecorder` (atomic with the trial insert). Carried the 2026-07-23 handoff doc in. PM review + adversarial (APPROVE-WITH-FIXES, no P0/P1) → fixes: atomic conditional-UPDATE immutability guard (TOCTOU close), fast-fail `param_grid_json` validation, freeze log after commit; Codex round 1 (2 P2) fixed — freeze side effect made atomic (same TOCTOU class), `allow_nan=False`. Full suite 475 passed. | Sonnet 5 medium: 74K (builder) + 62K (adversarial) + 55K (fix round) | Opus 4.8: heavy (owned the Codex-round-1 fixes per operator steer) |
| 04-2 (TrialRecorder) | `dev/R2-04-2-trial-recorder` | [#46](https://github.com/gumfactor/Agentic-Investing/pull/46) | **Merged 2026-07-23** (`314a527`). Wraps walk-forward/sweep; records every attempt (crash-safe running→errored); holdout-window guard + one-shot seal; NaN normalization; config-provenance + C7 `data_version`-shape checks. **5 Codex rounds, a legitimate data-leak P1 every round — all ONE class** ("guard validated a base date range dispatch could diverge from"): R1 post-holdout, R2 config-provenance + train/OOS containment, R3 touching-boundary (`< holdout_start`), R4 sweep date-override, R5 whole-`backtest`-section override. Closed with the GENERAL dot-path-ancestry rule (`903a752`), not instance patches — audited against `config_contract._BACKTEST_FIELDS` (start/end are the only window-moving keys) + fold-containment assertion. Operator merged. Follow-up (04-2 residual): confirmed `903a752` correct post-merge (142 passed). | Sonnet 5 medium: 85K (builder) + 68K (adversarial) + ~255K (5 fix rounds, incl. one dormant/PM-recovered) | Opus 4.8: heavy (R1/R2 fixes done directly pre-correction, 5-round polling/recovery) |
| 04-0 (design) + 04-1 (schema) | `dev/R2-04-1-selection-schema` | [#45](https://github.com/gumfactor/Agentic-Investing/pull/45) | **Merged 2026-07-22** (`f839d9a`). 04-2 (TrialRecorder) building next. Gate 04 Phase-0 design doc (rides with this PR) + slice 04-1 schema (migration 014: `strategy_hypotheses`/`strategy_trials`/`research_data_windows`/`promotion_decisions` + ORM). Review path: PM review → adversarial (real-Postgres attack pass, APPROVE-WITH-FIXES, no P0) → 4 Codex rounds. Codex found real issues each early round, all fixed: R1 P1 (holdout seal keyed only on `status='completed'` — errored-holdout re-read hole) + P1 the round before via adversarial (NaN backstop); R2 P1 (`window`/`run_type` not coupled — mislabeled holdout row escapes seal → added biconditional CHECK); R3 P2 (`selection_models` not imported on public `create_all` path); R4 P2 (hypotheses/data-windows `strategy_id` missing the definitions format regex). R3+R4 both clean of P0/P1 → stop condition met. Full `strategy_registry` suite **90 passed**. NOTE: live `alembic upgrade head` on real Postgres blocked this session (Docker engine down) — DDL compile-verified against PG dialect; operator's own migration-014 apply is the real-DB gate. **Pending operator: merge + `alembic upgrade head` (014).** | Sonnet 5 medium: ~209K (schema builder incl. adversarial-fix rounds + PR open) + 79K (adversarial reviewer) + 45K (R3 P2 fix) + 35K (R4 P2 fix) + 110K (Phase-0 design-doc builder, PR #45-adjacent) | Opus 4.8: ~430K (roadmap reconciliation, design-doc review, R1+R2 Codex fixes done directly before operator corrected to delegate, orchestration + own-poller babysit) |
| BUG-081 | `dev/R2-BUG081-paper-test-hygiene` | [#43](https://github.com/gumfactor/Agentic-Investing/pull/43) | **Merged 2026-07-22** (`e623d80`) — shared `tests/conftest.py` env/cwd isolation fixture (supersedes BUG-080's per-file fixture) + the real production root-cause fix (`now_fn` was threaded through `run()` but silently ignored by `_validate_blotter_freshness`, which called `datetime.now(UTC)` directly — fixed) + `paper_approve_blotter.py` documentation fix for its intentionally-real-wall-clock `approved_at`. Adversarial review found a genuine residual flake (identical BUG-080 failure signature recurred ~1-in-20 in reviewer testing); builder ran a 135-iteration reproduction effort, ruled out a SQLAlchemy-engine-state hypothesis via static audit, could not reproduce or fully root-cause it, and `bugs.md` honestly records BUG-081 as "significantly mitigated, not confirmed fully root-caused" rather than closed — leading unconfirmed hypothesis is Windows filesystem contention on the shared pytest temp directory (independently observed colliding with a parallel agent's test run this same session). Codex round 1 clean (0 findings); round 2 in progress at merge time — operator merged on the strength of the investigation. Full suite 2488 passed, 0 failed | Sonnet 5 medium: ~470K (builder incl. investigation + fix rounds) + 100K (adversarial reviewer) | Sonnet 5: ~40K |
| 03A-5 | `dev/R2-03A5-manifest-linkage` | [#44](https://github.com/gumfactor/Agentic-Investing/pull/44) | **Merged 2026-07-22** (`d71afd4`) — LAST 03A slice; completes the 03A group. Wires `eligibility_batch_id`/`membership_import_batch_id`/`research_methodology_id` FKs into `DatasetManifest`, `pin_snapshot.py` manifest→universe-batch linkage, and `data_version` cutover to `manifest_content_sha256` (hash-shape validation in `BacktestLogger`). Review path: adversarial (eligibility tiebreak + hash-shape regex) → Codex round 1 (raw-value hash validation + opt-in universe batch auto-link) → Codex round 2 (cross-check `research_methodology_id` against the resolved run's actual methodology). NOTE: a builder false-ready claim (2-min polling checks mistaken for 2 clean review rounds without an intervening re-summon) was caught this cycle — see [[pr-babysit-api-levels]]. Operator merged. | Sonnet 5 medium: ~250K (builder incl. review rounds) + ~70K (adversarial reviewer) | Sonnet 5: ~25K |
| 03A-4b | `dev/R2-03A4b-eligibility-population` | [#42](https://github.com/gumfactor/Agentic-Investing/pull/42) | **Merged 2026-07-21** (`6e1e7b9`) — BUG-078 Phase B Fixed. BUG-078 Phase B: daily `adv_usd_20d`/`price_usd` batch job, hand-curated `security_type` backfill (honest-empty seed data), `--strategy-config` scoring-path wiring (`scripts/backfill_momentum_scores.py`), coverage report. Process this round: internal adversarial review (APPROVE, 1 P2 fixed) → PR opened → **Codex is confirmed live for this repo (contrary to this round's initial assumption)** and ran 5 real review rounds, 4 of which surfaced genuine, repeatedly-substantive findings, ALL of the same underlying defect class — a computation or interval boundary silently bridging a real gap in the true trading-calendar sequence instead of failing closed: round 1 (`_chain_intervals` extended a value's coverage across a missing-data date instead of excluding it, same class as BUG-008/037/039), round 2 (default `security_type` row diverged from `PITUniverseLookup`'s knowledge-lag extension), round 3 (open-ended last row let a small corrective batch silently override a full-history batch under latest-`computed_at`-wins — the SAME rule as round 1, applied to the chain's final row), round 5 (ADV rolling window operated on row count rather than consecutive trading sessions, silently bridging a missing row inside the trailing window — a 4th instance of the same class). Round 4 came back clean (0 P0/P1, 3 P2s). Round cap (4) was reached before 2 consecutive clean rounds, so **PM ran a repo-wide sweep for the defect class** (never leave an inferred PIT boundary/window unanchored to the true calendar) and confirmed no further instance exists: only `data/universe/eligibility_batch.py` writes rows into the one per-row latest-batch-wins resolver in the repo (`data/universe/runtime.py:609`, Phase A); membership's resolver (`PITUniverseLookup`) picks one whole published batch, structurally immune to the same mixing bug. Operator reviewed the sweep and was comfortable proceeding without a mandatory 5th round; Codex's actual round 5 then arrived anyway and found the ADV-window instance above, which was fixed, tested, and pushed (`fa0726d`) per explicit operator sign-off to close out without a 6th summon. A separate independent-PR-style-review agent (deprecated for future rounds now that Codex is confirmed live) also ran and found 0 P0/P1, 5 P2/2 P3 (all fixed in the same rounds). Full suite (`data/tests/universe/` + 3 CLI-script test files): **277 passed, 0 failed** (final). BUG-082 filed (unrelated pre-existing momentum-scoring crash, out of scope, not fixed) | Sonnet 5 medium: 1524K (builder incl. all 5 Codex fix rounds) + 117K (adversarial reviewer) + 181K (independent PR-style reviewer, one-off) | Sonnet 5: ~170K (incl. post-round-cap defect-class sweep) |
| 01A | `dev/R2-01A-compose-runtime` | [#33](https://github.com/gumfactor/Agentic-Investing/pull/33) | **Merged 2026-07-16** (`3e37bb5`); **operator live verification complete 2026-07-18** — BUG-001..004 all `Fixed`. Gate 01A done; Gate 02A (no-submit DAG run) unblocked | Sonnet 5 medium: 284K (builder incl. fix + review rounds) + 122K (adversarial reviewer) | Fable 5: ~50K |
| 01B-1 | `dev/R2-01B1-missing-data` | [#32](https://github.com/gumfactor/Agentic-Investing/pull/32) | **Merged 2026-07-16** (`65e1b72`) | Sonnet 5 medium: 342K (builder incl. fix + review rounds) + 124K (adversarial reviewer) | Fable 5: ~45K |
| 01B-2 | `dev/R2-01B2-pit-universe` | [#34](https://github.com/gumfactor/Agentic-Investing/pull/34) | **Merged 2026-07-18** (`1df242e`) — BUG-008/BUG-067 Fixed; BUG-068/BUG-069 filed as residual data-quality/monitoring items (BUG-069 deferred, operator-accepted warn-degrade). Operator DB steps (migration 009 + universe import) still pending | Sonnet 5 medium: 494K (builder incl. all review rounds) + 167K (adversarial reviewer); PM completed final fix | Fable 5: ~75K |
| 02B | `dev/R2-02B-config-failclosed` | [#36](https://github.com/gumfactor/Agentic-Investing/pull/36) | **Merged 2026-07-20** (`bcdc29d`) — BUG-075 Fixed. Contract module + fail-closed validation at all 6 backtest entry points. Classification-vs-reads defect class surfaced across 3 passes and fixed each time: adversarial found `execution.*`/`name` (2 P0), builder sweep found `version`, Codex round 1 found `reporting.*` (2 P2, fixed `405208f`). Also caught a pre-existing test declaring `perfect` fills while running `transaction_cost`. Full suite 2267/2267 | Sonnet 5 medium: 219K (builder incl. all fix + babysit rounds) + 103K (adversarial reviewer) | Fable 5/Opus: ~55K |
| 03A-4a | `dev/R2-03A4a-pit-eligibility-schema` | [#41](https://github.com/gumfactor/Agentic-Investing/pull/41) | **Merged 2026-07-21** (`6b83bd3`) — BUG-078 Phase A. PIT eligibility schema (migration 013: `universe_eligibility_attributes`/`_batches`) + combined membership+eligibility read API + fail-closed config contract (market_cap rejected by name; adv_usd_20d/price_usd/security_type PIT-supported). Two review passes (Codex waived): adversarial APPROVE-WITH-FIXES (non-deterministic batch tie-break P1 + 3 fail-closed P2s) + Codex-equivalent APPROVE-WITH-FIXES (max_staleness_days latent-crash P2; verified migration↔ORM drift absent). Full suite 2415. **Pending operator: `alembic upgrade head` for migration 013.** Phase B = 03A-4b (data population) | Sonnet 5 medium: 197K (builder) + 83K (adversarial) + 34K (Codex-equivalent) | Fable 5/Opus: ~80K |
| 03B | `dev/R2-03B-backtester-series-split` | [#40](https://github.com/gumfactor/Agentic-Investing/pull/40) | **Merged 2026-07-20** (`37ffad3`) — BUG-070 Fixed. Backtester split into raw-execution series (fills/cash/shares + explicit split→share/dividend→cash accounting) and cutoff-aware analytic series; fail-closed on price-gap/missing corp-action data. Two review passes (Codex waived): adversarial APPROVE-WITH-FIXES (found the price-gap silent-drop P1) + Codex-equivalent APPROVE-WITH-FIXES (doc-consistency nit). Follow-ups: BUG-079 (wire analytic series into reporting), BUG-080 (test isolation, fixed), BUG-081 (systemic paper-test hygiene). Full suite 2368. Row 03B is a Gate-04 dependency | Sonnet 5 medium: 155K (builder) + 68K (adversarial) + 44K (Codex-equivalent) | Fable 5/Opus: ~75K |
| 03A-2 | `dev/R2-03A2-failclosed-objectstore` | [#39](https://github.com/gumfactor/Agentic-Investing/pull/39) | **Merged 2026-07-20** (`7f2fb8c`) — BUG-039 + BUG-077 Fixed. Fail-closed object-store error taxonomy: only genuine not-found is optional (behind explicit `allow_missing_corporate_actions`), all else (store-unavailable/access-denied/integrity/partial-read) aborts; single S3Error translation boundary + repo-wide containment test. Three review passes (Codex waived): hostile-adversarial APPROVE (fail-closed semantics) → save_manifest write-path P2 fix → Codex-equivalent broad review caught a read-path P1 (untranslated `response.read()` mid-stream failures) → fixed. Full suite 2355 | Sonnet 5 medium: 205K (builder) + 62K (adversarial) + 62K (Codex-equivalent) | Fable 5/Opus: ~70K |
| 03A-1 | `dev/R2-03A1-content-addressed-snapshots` | [#38](https://github.com/gumfactor/Agentic-Investing/pull/38) | **Merged 2026-07-20** (`98d206e`) — BUG-038 Fixed; also lands the 03A design plan. Content-addressed canonical-logical-hash snapshots + immutable manifest + load-time integrity (manifest root + leaf) + loader migration to manifest-driven reads + legacy date-keyed reader. Review path: adversarial REJECT (4 P0) → focused re-review APPROVE → **operator-requested HOSTILE review found a P0 the first two missed** (`load_manifest` had no integrity verification) + P1 hash-collision (non-injective encoding) → fixed → hostile re-verification APPROVE with executed proof. Codex WAIVED (rate-limited); internal review was the gate. P3 residual BUG-077 folded to 03A-2. Full suite 2324 | Sonnet 5 medium: 300K (builder incl. all fix rounds) + 62K + 79K (hostile review + re-verify) + 103K (original adversarial) + 46K (focused re-review) | Fable 5/Opus: ~110K |
| 03A-3 | `dev/R2-03A3-samedate-actions` | [#37](https://github.com/gumfactor/Agentic-Investing/pull/37) | **Merged 2026-07-20** (`6c28a85`) — BUG-037 Fixed. Same-date corporate-action product-of-multipliers accumulation + empirically-derived POST_SPLIT convention (AAPL retroactive-normalization evidence; operator-signed-off). Adversarial round (P1-2 fail-closed log distinguishability) + 2 clean Codex rounds (R1 pd.NA nullable-dtype, R2 convention DB-reachability documented). BUG-076 filed for residuals. Full suite 2279 | Sonnet 5 medium: 205K (builder incl. all rounds) + 62K (adversarial reviewer) | Fable 5/Opus: ~60K |
| 03A-0 | `dev/R2-03A-immutable-research-data` | none (design doc rides with first 03A implementation PR) | **Design plan delivered 2026-07-19** (`0d266e4` + PM-amendment commit `62d7add`: canonical logical content hash for object keys/manifests, same-date corporate-action convention normalization). PM approved; implementation gated on operator §6 answers | Sonnet 5 medium: 155K (incl. amendment round) | Fable 5: ~20K |
| 01B-3 | `dev/R2-01B3-timing-contract` | [#35](https://github.com/gumfactor/Agentic-Investing/pull/35) | **Merged 2026-07-19** (`71e6636`) — BUG-009 Fixed after 11 Codex review rounds; every P0/P1 resolved and verified (incl. a genuine realized-return PIT lookahead leak, round 3, and a consolidated methodology-honesty enforcement point, round 11). Operator merged before one final P2 (BUG-074, metadata precision) was triaged; filed as follow-up. Operator explicitly signed off on the BUG-069 write-path fail-closed behavior change | Sonnet 5 medium: 505K+ (builder incl. all 11 fix rounds) + 158K (adversarial reviewer) | Fable 5/Opus/Sonnet: ~130K |

Both adversarial reviews returned APPROVE-WITH-FIXES with confirmed findings
(01B-1: RSI-family EWM gap staleness and ungated A/D-line/Chaikin cumsum —
the BUG-010 defect class in non-`pct_change` form; 01A: opt-in runtime-marker
guard and unconsumed `IBKR_CLIENT_ID`); all findings were routed back to the
builders and fixed before the PRs opened. PM token figures are estimates of
the Fable 5 orchestration share attributable to each row.

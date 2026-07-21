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
| No | Make research data immutable and PIT-complete | P0 | 03A — Reproducible research | Data / Research | XL | Ready | Repair research-validity baseline | [BUG-037 to BUG-039](bugs.md) | Add effective-dated universe and eligibility data, immutable content-addressed snapshots, corporate-action preservation, and fail-closed object-store handling. | 2026-07-12 | |
| No | Split backtester prices into execution and analytic series | P1 | 03B — Reproducible research | Backtesting | M | Ready | Repair research-validity baseline | [BUG-070](bugs.md); [design plan §2](docs/plans/01b-research-validity-design.md) | Replace the single full-history adjusted series in `backtesting/loader.py` with a raw tradable execution series (fills, cash, share accounting) plus the cutoff-aware analytic builders from 01B-3 for signals and valuation; fail closed when corporate-action data is missing instead of assuming `adj_factor=1.0`. | 2026-07-19 | |
| No | Establish a real strategy-selection protocol | P1 | 04 — Research qualification | Strategy Validation | XL | Blocked | Make research data immutable and PIT-complete; Split backtester prices into execution and analytic series; Fail closed on unsupported strategy configuration | [Backtesting validation](backtesting/validation); residual caveats [BUG-066, BUG-068, BUG-071](bugs.md) | Record hypotheses and trials, select only inside training windows, freeze configuration, test out of sample, retain a final holdout, and capture all variants for multiple-testing analysis. | 2026-07-12 | |
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

Gate 01 is complete: 01A was operator-verified live on 2026-07-18 and the 01B
research-validity baseline (BUG-008/009/010) was delivered on 2026-07-19. Do
not begin the four-week automated paper clock, and continue to treat current
backtest results as non-evidence for strategy selection — BUG-070 (row 03B)
leaves the backtester's price handling contaminated until fixed.

The active front is Gate 02 with the Gate 03 research track running in
parallel, mirroring the 01A/01B split (platform and research touch disjoint
systems): 02A is the operator-run no-submit Compose proof (entry condition:
migration 009 + PIT universe import, still pending from 01B-2), 02B is the
fail-closed strategy-configuration slice, and 03A/03B are ready for builder
sequencing. Gate 04 must not start until 02B, 03A, and 03B are all delivered.

## Delivery execution log (R2 round)

Managed by the project-manager session. Integration branch: `dev/R2-phase1`.
Task branches are named `dev/R2-<order>-<slug>`. Each phased slice is one
commit; each roadmap job (or sub-job below) is one PR into `dev/R2-phase1`.
Token figures are reported as `<agent/effort>: <tokens>`.

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

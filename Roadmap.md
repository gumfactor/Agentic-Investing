# RQIS Current Delivery Roadmap

**Current baseline:** `main` at `39c5e5b` (2026-06-30). This roadmap was rebuilt on 2026-07-12 from the synced repository, current `bugs.md`, `CLAUDE.md`, `Worklog.md`, and three independent reviews of execution, strategy/research, and dashboard/operator experience.

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
| No | Make Compose paper runtime executable | P0 | 01A — Runtime foundation | Platform | L | Ready | None | [BUG-001 to BUG-004](bugs.md) | Pass all required paper/IBKR settings into Airflow, install DAG runtime dependencies, mount the shared artifact directory, and configure host-to-IBKR connectivity. | 2026-07-12 | |
| No | Repair research-validity baseline | P0 | 01B — Research foundation | Research | XL | Ready | None | [BUG-008 to BUG-010](bugs.md) | Replace current-membership historical universes, define and enforce signal-to-trade timing, and remediate unsafe missing-data defaults across the indicator library. | 2026-07-12 | |
| No | Prove the Compose no-submit workflow | P0 | 02A — Runtime proof | Platform | L | Blocked | Make Compose paper runtime executable | [DAG runbook](docs/runbooks/airflow_fire_drill.md) | Build the images, apply migrations, import both DAGs, verify shared artifacts and IBKR reachability, then complete a no-submit DAG run with retained evidence. | 2026-07-12 | |
| No | Fail closed on unsupported strategy configuration | P0 | 02B — Semantic proof | Strategy Correctness | L | Blocked | Repair research-validity baseline | [v2 strategy config](config/strategy/v2_mvo_momentum.yaml) | Implement declared MVO/risk-parity/constraint semantics in the backtester or reject unsupported fields; add a consumed-field conformance test for every strategy config key. | 2026-07-12 | |
| No | Make research data immutable and PIT-complete | P0 | 03 — Reproducible research | Data / Research | XL | Blocked | Repair research-validity baseline | [BUG-037 to BUG-039](bugs.md) | Add effective-dated universe and eligibility data, immutable content-addressed snapshots, corporate-action preservation, and fail-closed object-store handling. | 2026-07-12 | |
| No | Establish a real strategy-selection protocol | P1 | 04 — Research qualification | Strategy Validation | XL | Blocked | Make research data immutable and PIT-complete; Fail closed on unsupported strategy configuration | [Backtesting validation](backtesting/validation) | Record hypotheses and trials, select only inside training windows, freeze configuration, test out of sample, retain a final holdout, and capture all variants for multiple-testing analysis. | 2026-07-12 | |
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

Do not begin the four-week automated paper clock and do not treat current
backtest results as strategy-selection evidence. The immediate work is Gate 01:
make the deployed paper system runnable and repair research validity in parallel.

## Delivery execution log (R2 round)

Managed by the project-manager session. Integration branch: `dev/R2-phase1`.
Task branches are named `dev/R2-<order>-<slug>`. Each phased slice is one
commit; each roadmap job (or sub-job below) is one PR into `dev/R2-phase1`.
Token figures are reported as `<agent/effort>: <tokens>`.

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
| 01A | `dev/R2-01A-compose-runtime` | [#33](https://github.com/gumfactor/Agentic-Investing/pull/33) | PR open — babysitting review | Sonnet 5 medium: 260K (builder incl. fix round) + 122K (adversarial reviewer) | Fable 5: ~35K |
| 01B-1 | `dev/R2-01B1-missing-data` | [#32](https://github.com/gumfactor/Agentic-Investing/pull/32) | **Ready for operator review/merge** — review rounds clean, 778 signals + 249 backtesting/portfolio tests green on tip `309539d` | Sonnet 5 medium: 342K (builder incl. fix + review rounds) + 124K (adversarial reviewer) | Fable 5: ~45K |
| 01B-2 | `dev/R2-01B2-pit-universe` | — | Queued (blocked on PR #32 merge — overlapping research paths) | — | — |
| 01B-3 | `dev/R2-01B3-timing-contract` | — | Queued (blocked on 01B-2) | — | — |

Both adversarial reviews returned APPROVE-WITH-FIXES with confirmed findings
(01B-1: RSI-family EWM gap staleness and ungated A/D-line/Chaikin cumsum —
the BUG-010 defect class in non-`pct_change` form; 01A: opt-in runtime-marker
guard and unconsumed `IBKR_CLIENT_ID`); all findings were routed back to the
builders and fixed before the PRs opened. PM token figures are estimates of
the Fable 5 orchestration share attributable to each row.

# Adversarial Review Findings

Review date: 2026-06-30

This file consolidates an adversarial, multi-theme review of the project. It is intentionally written as a handoff queue for later remediation; no fixes are included here.


## How to use this running tally

`bugs.md` is the canonical running tally of known fixes that need to happen before this system can be considered hardened for larger paper-trading scale or any live-capital discussion. Treat it as a living remediation queue:

- Add every newly discovered defect, weakness, or deferred fix as a numbered `BUG-XXX` entry.
- Do not delete fixed items silently. When a bug is fixed, mark its status in the roadmap and reference the fixing PR/commit so the audit trail remains intact.
- Keep each item classified by category, severity, implementation priority, and short/medium/long-term fix horizon.
- Re-triage after each major milestone, adversarial review, or production/paper incident.

### Triage taxonomy

| Field | Values | Meaning |
|-------|--------|---------|
| Category | `Infra/Deploy`, `Trading Safety`, `Security/Auth`, `Dashboard/API`, `Risk`, `Research/Signals`, `Data/Storage`, `Backtesting`, `Portfolio`, `Packaging/CI`, `Docs/Process` | Primary subsystem or ownership area. |
| Severity | `P0`, `P1`, `P2`, `P3` | User/system impact if unfixed. `P0` blocks safe operation or invalidates core results; `P1` is high impact; `P2` is medium impact; `P3` is low impact. |
| Fix priority | `F0`, `F1`, `F2`, `F3` | Implementation ordering. `F0` is immediate stop-the-line work; `F1` should be next-sprint hardening; `F2` is planned medium-term work; `F3` is backlog/cleanup. |
| Horizon | `Short`, `Medium`, `Long` | Short-term fixes should happen before the next serious paper/live operational expansion; medium-term fixes belong in the next hardening phase; long-term fixes are lower-risk backlog items. |
| Status | `Open`, `In Progress`, `Implemented — pending operator verification`, `Fixed`, `Deferred`, `Won't Fix` | Current remediation state. New entries default to `Open`. `Implemented — pending operator verification` means the code/test change is complete and merged to the working branch, but a step requiring a live external dependency (e.g. a real TWS/IB Gateway session) that an automated agent session must not perform is still outstanding before the item can be marked `Fixed`. |

### Fix implementation roadmap

#### Short-term / stop-the-line and near-term hardening

| Bug | Category | Severity | Fix priority | Status | Short rationale |
|-----|----------|----------|--------------|--------|-----------------|
| BUG-001 | Infra/Deploy | P0 | F0 | Fixed | Airflow paper DAG cannot pass its own env gate in Compose. |
| BUG-002 | Infra/Deploy | P0 | F0 | Fixed | Airflow image omits runtime dependencies used by DAGs. |
| BUG-003 | Infra/Deploy | P0 | F0 | Fixed | Paper artifacts are written to an unmounted container path. |
| BUG-004 | Infra/Deploy | P0 | F0 | Fixed | IBKR host defaults to container-local localhost. |
| BUG-005 | Trading Safety | P0 | F0 | Fixed | Approval quantity overrides can be tampered upward. |
| BUG-006 | Trading Safety | P0 | F0 | Fixed | Corrupt reconciliation artifacts can cause duplicate orders. |
| BUG-007 | Risk | P0 | F0 | Fixed | Risk dashboard can report zero/incorrect risk from schema mismatch. |
| BUG-008 | Research/Signals | P0 | F0 | Fixed | Current-membership universe creates survivorship leakage. |
| BUG-009 | Research/Signals | P0 | F0 | Fixed | Same-close signal/return timing can introduce lookahead. |
| BUG-010 | Research/Signals | P0 | F0 | Fixed | `pct_change()` defaults can distort many indicators. |
| BUG-011 | Security/Auth | P1 | F1 | Open | Approval gate trusts any matching DB row. |
| BUG-012 | Trading Safety | P1 | F1 | Open | Circuit breaker is UI-local and not enforced by Airflow submission. |
| BUG-013 | Security/Auth | P1 | F1 | Open | Host-published services and weak auth create compromise paths. |
| BUG-014 | Security/Auth | P1 | F1 | Open | Dashboard approval identity is spoofable/unknown. |
| BUG-015 | Dashboard/API | P1 | F1 | Open | Blotter UI can approve the wrong pending run. |
| BUG-016 | Dashboard/API | P1 | F1 | Open | Blotter UI does not validate full schema before approval. |
| BUG-017 | Trading Safety | P1 | F1 | Fixed | Quantity reduction updates one field while validation checks another. |
| BUG-036 | Packaging/CI | P0 | F0 | Fixed | Invalid PEP 517 backend blocks package builds. |
| BUG-037 | Data/Storage | P1 | F1 | Fixed (PR #37, merged 2026-07-20) | Same-date corporate actions overwrite one another. Fixed: product-of-multipliers accumulation + POST_SPLIT convention (operator-signed-off). Residuals tracked in BUG-076. |
| BUG-038 | Data/Storage | P1 | F1 | Fixed (PR #38, merged 2026-07-20) | Snapshot version paths are mutable. Fixed: content-addressed canonical-logical-hash object keys + immutable manifest + load-time integrity verification (manifest and leaf). Hostile review hardened injective encoding + manifest-root verification. |
| BUG-039 | Backtesting | P1 | F1 | Fixed (03A-2, branch `dev/R2-03A2-failclosed-objectstore`, PR pending) | Object-store failures can become unadjusted backtests. |
| BUG-040 | Trading Safety | P1 | F1 | Fixed | Wash-sale guard checks the wrong order direction. |
| BUG-055 | Trading Safety | P0 | F0 | Fixed | prices_json=None crashes _write_simulation before error handler, blocking ExternalTaskSensor. |
| BUG-056 | Docs/Process | P1 | F1 | Fixed | wash_sale_context docstring says "SELL-side tickers" after BUG-040 fix renamed to BUY-side. |
| BUG-057 | Trading Safety | P1 | F1 | Fixed | bool is int subclass; True passes isinstance(override_qty, int) and submits 1 share silently. |
| BUG-058 | Trading Safety | P1 | F1 | Fixed | Second reconciliation artifact read swallows exceptions, truncating audit trail on retry. |
| BUG-059 | Research/Signals | P1 | F1 | Fixed | simulated_return divides by len(returns) not n_long, overstating NAV when tickers lack prior-day data. |
| BUG-060 | Trading Safety | P1 | F1 | Fixed | No test for fail-safe BUY rejection when recent_loss_sells set but as_of_date absent. |
| BUG-041 | Risk | P1 | F1 | Open | Sector concentration is computed but not breach-checked. |
| BUG-042 | Trading Safety | P1 | F1 | Open | IBKR order-ID timeout can leave live order untracked. |
| BUG-043 | Packaging/CI | P1 | F1 | Open | Test collection can fail through MLflow/pkg_resources drift. |
| BUG-044 | Packaging/CI | P1 | F1 | Open | Package discovery excludes operational modules. |
| BUG-045 | Packaging/CI | P1 | F1 | Open | Local Airflow stubs shadow real Airflow imports. |
| BUG-078 | Research/Signals | P1 | F1 | Phase A merged (PR #41, 2026-07-21); Phase B implemented, pending review (`dev/R2-03A4b-eligibility-population`) | Strategy-config eligibility filters (market cap, ADV, price, security type) had no PIT source and were silently unenforced/substitutable with current values, violating 01B §1.3. Phase A: PIT eligibility schema + runtime read API + fail-closed config contract rejecting `min_market_cap_usd` by name. Phase B (this): daily batch job populating `adv_usd_20d`/`price_usd` from `daily_prices`, hand-curated `security_type` backfill, `--strategy-config` wiring of the combined membership+eligibility check into `scripts/backfill_momentum_scores.py`, and a coverage report. See full entry below. |
| BUG-082 | Signals/Backtesting | P2 | F2 | Open | `scripts/backfill_momentum_scores.py` raises an unhandled `KeyError: 'date'` instead of a clean "0 rows" dry-run/write outcome when the PIT eligibility/membership cross-section excludes every candidate ticker for every score date in the requested range (discovered while testing 03A-4b's `--strategy-config` wiring with a deliberately impossible threshold). Root cause is in the shared momentum-scoring path: `signals.composites.momentum_score.compute_momentum_scores` appears to return a DataFrame without a `date` column when its input eligibility mask empties the whole cross-section, and `run()` unconditionally indexes `momentum_df["date"]` afterward with no empty-input guard. Not a 03A-4b-introduced defect (a membership-only over-restrictive universe could already have triggered it pre-03A-4b) and not fixed in this slice to avoid scope creep into the shared signals module; an operator hitting an all-excluded run should get a clear "0 eligible tickers for the requested range" error instead of a raw KeyError. |

#### Medium-term / planned hardening

| Bug | Category | Severity | Fix priority | Status | Short rationale |
|-----|----------|----------|--------------|--------|-----------------|
| BUG-018 | Infra/Deploy | P1 | F2 | Open | Project Python metadata conflicts with Airflow image. |
| BUG-019 | Infra/Deploy | P1 | F2 | Open | MLflow bucket config is ignored by server command. |
| BUG-020 | Data/Storage | P1 | F2 | Open | Raw snapshot path logging ignores custom bucket names. |
| BUG-021 | Infra/Deploy | P1 | F2 | Open | Alembic migrations assume extensions exist externally. |
| BUG-022 | Infra/Deploy | P1 | F2 | Open | DB init scripts are one-shot but required for secondary DBs. |
| BUG-023 | Research/Signals | P1 | F2 | Open | PEG inverse rewards double-negative EPS/growth cases. |
| BUG-024 | Research/Signals | P1 | F2 | Open | Fundamental PIT safety is delegated to input dates. |
| BUG-025 | Research/Signals | P1 | F2 | Open | Missing-data renormalization creates coverage-driven alpha. |
| BUG-026 | Research/Signals | P1 | F2 | Open | Indicator z-scoring drops all-tie/one-name dates. |
| BUG-027 | Dashboard/API | P2 | F2 | Open | Approval UI can crash on malformed/null quantities. |
| BUG-028 | Dashboard/API | P2 | F2 | Open | Artifact scanning lacks strong containment checks. |
| BUG-029 | Trading Safety | P2 | F2 | Open | Live-clearance env var names differ across components. |
| BUG-030 | Trading Safety | P2 | F2 | Fixed | Airflow retries are risky for broker submission/reconcile/ledger tasks. |
| BUG-031 | Research/Signals | P2 | F2 | Open | Fundamental growth uses daily-row shifts after forward-fill. |
| BUG-032 | Data/Storage | P2 | F2 | Open | `pivot_table()` silently averages duplicate records. |
| BUG-033 | Infra/Deploy | P2 | F2 | Open | Prometheus scrape target is not backed by Compose service. |
| BUG-046 | Data/Storage | P2 | F2 | Open | Market-data backfill can mark partial loads complete. |
| BUG-047 | Data/Storage | P2 | F2 | Open | Data-quality flag dedupe has no conflict key. |
| BUG-048 | Trading Safety | P2 | F2 | Open | Trade-fill dedupe allows duplicate cumulative fills. |
| BUG-049 | Portfolio | P2 | F2 | Open | Optimizer fallbacks can violate configured caps. |
| BUG-050 | Risk | P2 | F2 | Open | NaN-heavy return series can suppress VaR/CVaR breaches. |
| BUG-051 | Trading Safety | P2 | F2 | Fixed | Step 7 CLI can submit old checksum-valid blotters. |
| BUG-052 | Docs/Process | P2 | F2 | Fixed | Fire-drill runbook contradicts DAG timezone semantics; inline schedule_interval comment also wrong. |
| BUG-053 | Packaging/CI | P2 | F2 | Fixed | `make check` mutates the working tree. |
| BUG-064 | Research/Signals | P2 | F2 | Fixed | `_write_simulation` only processes current-run strategy from XCom; shadow strategies are skipped. |
| BUG-065 | Research/Signals | P2 | F2 | Fixed | `simulated_return` divides by n_long when universe < n_long, understating returns for small strategies. |
| BUG-066 | Research/Signals | P2 | F2 | Open | Cross-sectional scoring has no minimum-eligible-count gate; full-window suppression increases silent cross-section shrinkage. |
| BUG-067 | Data/Universe | P1 | F1 | Fixed (dev/R2-01B2-pit-universe) | `config/universe_loader.py` returned an empty universe on Wikipedia fetch failure (fail-open). |
| BUG-068 | Data/Universe | P2 | F2 | Open | Wikipedia constituent history has bounded count drift (left-censored inflation ~3% recent era; sparse pre-2000 changes; 3 ticker-collision exclusions). |
| BUG-069 | Data/Universe | P2 | F2 | Deferred (operator-accepted 2026-07-18) | daily_signal_pipeline degrades to unfiltered provisional scores when the PIT universe import is missing/stale; no alert beyond a log warning. |
| BUG-070 | Backtesting | P1 | F2 | Fixed (PR #40, merged 2026-07-20) | Backtester loaded a single full-history adjusted price series for both scoring and execution. Fixed: split into a raw-execution series (fills/cash/shares) with explicit split→share / dividend→cash accounting, and a cutoff-aware analytic series; fail-closed on price-gap/missing corp-action data. Analytic-series reporting wiring tracked as BUG-079. |
| BUG-071 | Research/Signals | P2 | F2 | Open | IC-validation cutoff-aware adjustment uses one run-boundary cutoff, not a literal per-score-date cutoff (documented residual). |
| BUG-072 | Dashboard/API | P2 | F2 | Fixed | All alpha/factor-score readers (dashboards, `scripts/indicator_diagnostic.py`) now filter to the active research run by default; `--all-runs`/`--research-run-id` are the only documented explicit opt-ins for cross-run reads. |
| BUG-073 | Packaging/CI | P1 | F1 | Fixed | `pyproject.toml`'s pytest `testpaths` silently excluded ~412 tests (all of `tests/reporting/dashboards/`, `tests/infra/`) from every "full suite" run whenever a subdirectory (`tests/strategy_registry`) was also listed as its own testpath entry. |
| BUG-074 | Research/Signals | P2 | F2 | Open | Registered operational methodology labels action_source_version as plain "unknown", imprecise for a DB with migrated legacy corporate_actions rows tagged "legacy_unknown". |
| BUG-075 | Backtesting | P0 | F0 | Fixed (PR #36, merged 2026-07-20) | Backtest path silently ignored strategy-config fields it does not implement (`portfolio.method: mvo`/`risk_parity`, `optimizer_mode`, `constraints`, `risk_model`, live-only `execution` fields) instead of rejecting them — a backtest labeled "mvo with sector caps" was actually an uncapped equal-weight backtest. |
| BUG-076 | Data/Storage | P2 | F2 | Open | Residual of BUG-037: same-date ordinary split+dividend boundary untested against a real row; Yahoo spinoffs modeled as same-date split+dividend rows are normalized as ordinary split+dividend. Includes P2 sub-notes on silently-ignored NaN dividend/zero split ratio and the §2.3×§3.1 cutoff/convention interaction. |
| BUG-077 | Data/Storage | P3 | F3 | Fixed (03A-2, branch `dev/R2-03A2-failclosed-objectstore`) | `load_manifest`'s content-addressed predicate is `^[0-9a-f]{64}$`, so an uppercase-hex or 64-char-non-hex `data_version` silently drops to the legacy unverified path. Not exploitable (pipeline emits only lowercase hexdigest; MinIO keys are case-sensitive so an uppercase key cannot alias the genuine object). Fixed by `_is_malformed_hash_version()`: a `version` exactly 64 characters long that fails the canonical lowercase-hex regex now raises `ValueError` before any network call, rather than silently falling to the unverified `legacy_mutable` path; genuine legacy date-string versions (10 characters) are unaffected. Tests: `backtesting/tests/test_dataset_manifest.py::test_uppercase_hex_version_is_rejected_not_treated_as_legacy`, `::test_64_char_non_hex_version_is_rejected_not_treated_as_legacy`, `::test_genuine_legacy_date_version_is_unaffected_by_bug_077_guard`. Found by 03A-1 hostile re-verification (PR #38). |
| BUG-079 | Backtesting/Reporting | P2 | F2 | Open (follow-up to BUG-070, 2026-07-20) | The cutoff-aware total-return ANALYTIC price series added by BUG-070 (`DataHandler.get_analytic_close`, built in `backtesting/loader._build_analytic_prices` via `build_realized_total_return_as_of`) is available and tested but consumed by NOTHING — no tearsheet/attribution/report reads it. Backtester NAV is already total-return-correct via raw-price + explicit dividend-cash/split-share accounting, so this is scaffolding for future reporting, not a NAV-correctness gap. Wire the analytic/total-return series into a reporting/attribution consumer (e.g. a tearsheet total-return overlay or an attribution series that must use the adjusted, not raw, price) so the contract is exercised end-to-end. Scoped deliberately OUT of the BUG-070 slice to avoid ballooning it. (Renumbered from BUG-078, which the parallel 03A-4a branch had already claimed for PIT eligibility.) |
| BUG-080 | Testing/CI | P2 | F2 | Partially superseded by BUG-081 (dev/R2-BUG081-paper-test-hygiene, 2026-07-21) | Order-dependent cross-test global-state leak: `tests/test_paper_stage_blotter_check.py::test_run_writes_stage_only_blotter_after_step_five_passes` fails only under certain full-suite collection orders (passes in isolation, as a module, and under other orders). Root cause is pre-existing leaked process-global state (`os.environ` — `paper_stage_blotter_check.run` calls `load_dotenv()` which mutates `os.environ`; and/or cwd) from an earlier test that fails to clean up; the 03B backtester-series-split tests shifted collection order enough to expose it. Originally fixed defensively with a one-off autouse fixture in the paper-stage-blotter test module only. That per-file fixture has been retired in favor of the shared `tests/conftest.py` fixture added under BUG-081, which covers the same env/cwd ground for every `tests/test_paper_*.py` module, not just this one. **However**, adversarial re-review of the BUG-081 fix reproduced a failure with the identical signature (the same 4 tests in `test_paper_stage_blotter_check.py`, including the originally cited one) once in ~19-20 attempts with `test_paper_run_audit_check.py` run immediately before it — see BUG-081 for the follow-up investigation. The env/cwd leak this entry describes is confirmed fixed; whatever caused that one residual recurrence is a distinct, still-unconfirmed cause tracked under BUG-081. |
| BUG-081 | Testing/CI | P2 | F2 | Significantly mitigated, not confirmed fully root-caused (dev/R2-BUG081-paper-test-hygiene, 2026-07-21) | Systemic paper-path test fragility to process-global and wall-clock state. Two instances originally surfaced during the parallel 03B/03A-4a wave: (1) the env/cwd order-dependent leak fixed per-file in BUG-080 (`test_paper_stage_blotter_check.py`); (2) `test_paper_submit_reconcile_check.py::test_confirm_yes_submits_with_fake_broker_and_writes_reconciliation` flaked once on a midnight `date.today()` boundary (calendar rolled 2026-07-19→20 mid-run), passing in isolation and on re-run. **Confirmed fixed:** (a) an autouse `tests/conftest.py` fixture, scoped narrowly to `tests/test_paper_*.py` by filename (not blanket-applied to the ~2400-test suite), snapshots/restores `os.environ` and cwd around every paper-path test, superseding the BUG-080 per-file fixture; (b) the actual root cause of the date.today()-boundary flake — `paper_submit_reconcile_check.run()` already accepted a `now_fn` override used for every submission timestamp, but `_validate_blotter_freshness()` ignored it and called `datetime.now(UTC)` directly — is fixed by threading `now_fn` through to the freshness check, plus shared `frozen_now_utc`/`frozen_now_fn` fixtures (following this repo's existing `today_fn`/`now_fn` injection convention rather than `freezegun`) applied to every freshness/boundary test in that file, not just the cited one. **Not confirmed root-caused — residual risk remains open:** adversarial re-review of this fix, running the full paper-path suite ~19-20 times with varied collection order/`PYTHONHASHSEED`, caught ONE recurrence of the exact original BUG-080 failure signature (`test_run_writes_stage_only_blotter_after_step_five_passes` plus 3 sibling tests in `test_paper_stage_blotter_check.py`, all failing together) with `test_paper_run_audit_check.py` running immediately before it — i.e. the env/cwd fix reduces the leak's frequency but did not eliminate every case. Follow-up investigation (same branch) could not reproduce it again in 135 further attempts (15 runs of the exact two-file pairing under varied `PYTHONHASHSEED`; 40 + 80 runs of the full 12-file `tests/test_paper_*.py` set in randomly shuffled collection order with varied hash seeds) — all 135 clean. A static audit of every script/test module in the failing pair (`scripts/paper_run_audit_check.py`, `scripts/paper_stage_blotter_check.py`, `scripts/paper_inputs_check.py`'s shared `CheckRecorder`, and `data/research/identity.py`/`data/research/models.py`'s shared declarative `Base.metadata` used by the `tests/_research_run_test_helpers.py::setup_active_research_run` helper both files call) found no `lru_cache`, module-level mutable state, or singleton cache — every test builds its own isolated `create_engine("sqlite://")` in-memory database and all "active research run" resolution is DB-backed per-engine, not process-global; `CheckRecorder` is a plain per-instance object. The one process-global touch point found — two tests (`test_paper_run_audit_check.py` and `test_paper_stage_blotter_check.py`) each `monkeypatch.setattr(check.os, "link", racing_link)` to simulate a write race — should not leak, since pytest's `monkeypatch` fixture reverts via its own finalizer regardless of test outcome. Leading (unconfirmed) hypothesis: the failure signature is specific to the 4 tests in that file that exercise real file I/O through `_write_artifact`'s temp-file-then-`os.link`/`Path.replace` dance under `tmp_path`, and this session's own investigation independently hit a transient Windows `PermissionError: Access is denied` on the shared `%TEMP%\pytest-of-<user>` base directory (unrelated to this repo's code) while multiple `python.exe`/pytest processes were running concurrently on the same machine — plausibly another agent's parallel test run. This points toward Windows filesystem-level contention (AV/indexer scanning, or concurrent-process temp-directory locking) rather than a persisting in-process Python global-state leak, but this has not been proven and is not ruled in either. **Status is intentionally not "Fixed":** the confirmed env/cwd and now_fn fixes should be kept, but do not claim this defect class is fully closed. Follow-up: if this signature recurs, capture the raw pytest traceback (not just PASSED/FAILED) for the exact failing assertion/exception to confirm or rule out the Windows I/O-contention hypothesis; consider adding a short retry-on-`PermissionError` wrapper around `_write_artifact`'s `os.link`/`Path.replace` calls if the hypothesis is confirmed. Verified (for the fixes that are confirmed) with the full suite run twice under two different collection orders (default alphabetical order, and a full reverse-file-order pass); see Worklog for exact pass counts. |

#### Long-term / lower-risk backlog

| Bug | Category | Severity | Fix priority | Status | Short rationale |
|-----|----------|----------|--------------|--------|-----------------|
| BUG-034 | Dashboard/API | P3 | F3 | Open | Performance table formats decimal returns as percentages incorrectly. |
| BUG-035 | Dashboard/API | P3 | F3 | Open | No FastAPI/API route layer exists despite service-boundary expectations. |
| BUG-054 | Data/Storage | P3 | F3 | Open | Fundamentals backfill skip logic can leave partial ingestions stale. |
| BUG-061 | Docs/Process | P3 | F3 | Fixed | deferred_items.md RESOLVED entry used stale recent_loss_buys key after BUG-040 rename. |
| BUG-062 | Trading Safety | P3 | F3 | Fixed | Override cap validated against estimated_shares while submission uses quantity field (inconsistent). |
| BUG-063 | Packaging/CI | P3 | F3 | Fixed | __import__("json").dumps() antipattern in _write_simulation; replaced with proper import json. |

## Startup / repository state

- Current branch observed: `work`.
- No configured `origin` remote or `origin/<branch>` tracking branch was visible from `git remote -v` / branch checks at review startup.

## P0 / Critical findings

### BUG-001: Airflow paper-trading DAG cannot pass its own environment gate under Docker Compose

**Severity:** P0 / deployment blocker

**Evidence:** `docker-compose.yml` passes database, Redis, MinIO, and Polygon settings into Airflow, but does not pass `PAPER_TRADING`, `IBKR_PORT`, `IBKR_HOST`, or `IBKR_CLIENT_ID`. The paper DAG fails fast unless `PAPER_TRADING=true` and `IBKR_PORT=7497` are present. `.env.example` defines those variables, but they are not injected into the Airflow service environment.

**Impact:** The Compose-managed `daily_paper_trading` DAG will fail before performing useful work.

**Suggested direction:** Pass the IBKR/paper env vars into all Airflow containers, and add a deployment smoke test that imports the DAG and runs `_require_paper_env()` against the container environment.

**Resolution (Gate 01A, branch `dev/R2-01A-compose-runtime`):** `PAPER_TRADING`,
`IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, and `RQIS_PAPER_ARTIFACT_DIR` are
now set in `docker-compose.yml` `x-airflow-common.environment`, reaching
`airflow-init`, `airflow-webserver`, and `airflow-scheduler` identically.
Verified with `docker compose config` against `.env.example` placeholders:
`PAPER_TRADING=true`, `IBKR_PORT=7497`,
`RQIS_PAPER_ARTIFACT_DIR=/opt/airflow/rqis_paper` on all three services.
`tests/infra/test_compose_paper_runtime.py` asserts this both statically and
(when Docker is available) via a real `docker compose config` render.
Adversarial fix round (same branch): `PAPER_TRADING` and `IBKR_PORT` are
now HARD-CODED to paper values in `x-airflow-common` rather than
`.env`-substituted (a stale live-side `.env` can no longer render
`PAPER_TRADING=false`/`IBKR_PORT=7496` into the Airflow services — P2-4,
with a rendering regression test), `IBKR_CLIENT_ID` is now actually consumed
by `IBKRBroker.__init__` via a validated env default instead of being
declared-but-ignored (P1-2), and `_require_paper_env` in the DAG now also
requires `RQIS_RUNTIME_CONTEXT=compose_bridged` so a runtime that bypassed
the reviewed Compose contract fails closed at the first task (P1-1).
**Status: Fixed.** Operator-verified 2026-07-18 per
`docs/runbooks/01a_compose_paper_runtime_verification.md`: `docker compose
config` confirmed the paper-runtime variables render on all three Airflow
services against a live `.env`; `docker compose ps` showed
`airflow-webserver`/`airflow-scheduler` healthy and `airflow-init` exited
`0`. (One unrelated environment issue was hit and fixed en route: Docker Hub
had pruned the pinned `minio/mc` client tag, blocking `docker compose up`
for the whole stack; repointed to `minio/mc:latest` — see `Worklog.md`
2026-07-18.)

### BUG-002: Airflow image omits runtime dependencies used by DAG execution paths

**Severity:** P0 / deployment blocker

**Evidence:** `infra/docker/Dockerfile.airflow` installs only a short hand-picked package list, while DAG execution paths import `pandas`, `pyarrow`, `ib_insync`, database drivers, and other packages listed in `requirements.txt`.

**Impact:** The built Airflow runtime is likely to fail at task runtime with `ModuleNotFoundError`, especially for Parquet/MinIO and IBKR paper-trading paths.

**Suggested direction:** Install the project package and/or `requirements.txt` in the Airflow image, then test DAG imports inside the built image.

**Resolution (Gate 01A, branch `dev/R2-01A-compose-runtime`):**
`infra/docker/Dockerfile.airflow` now documents and installs only what is
actually missing (`ib-insync`, `minio`, `structlog`, `yfinance` -- pandas,
numpy, pyarrow, PyYAML, python-dotenv, psycopg2-binary, requests, and lxml
already ship in the base image at Airflow's own constraints versions) and
adds a build-time gate that fails the build if any Airflow-critical package
drifts from the base image or if `pip check` reports anything beyond one
documented, unused-provider mismatch (`snowflake-connector-python`
vs. `cffi`). A full `pip install --constraint <Airflow constraints>` was
tried first and rejected because it produces a genuine
`ResolutionImpossible` (yfinance's `curl_cffi` needs `cffi>=2.0`; Airflow's
constraints pin `cffi==1.16.0` for the unused snowflake provider) -- this is
recorded in the Dockerfile comments rather than masked. Python version
decision: Airflow 2.8.1 has no Python 3.12 image or constraints file
(verified directly against Docker Hub and
`constraints-2.8.1/constraints-3.12.txt`, which 404s), so the image stays on
Python 3.11; this is a documented, known gap against the project's
`requires-python>=3.12`, not an oversight.
`infra/docker/smoke_test_dag_imports.py` imports `daily_paper_trading`,
`daily_signal_pipeline`, `daily_data_pipeline`, and every module reached
through the C1 approval gate inside the built image and asserts
`airflow.__file__` resolves to the installed package, not this repo's
`airflow/` test-stub. `tests/infra/test_airflow_image_smoke.py` wires a full
`docker build` + smoke-run into pytest (skips without Docker). Verified
locally end-to-end with Docker Desktop 29.6.1 / Compose v5.2.0: build
succeeds, `airflow version` reports `2.8.1`, and all 20 target modules
import with exit 0. Adversarial fix round (same branch): the build-time
`pip check` gate now also fails on an abnormal pip exit code or unexpected
stderr output (previously a pip internal error with empty stdout would have
passed silently — P2-1), and the allowlist anchors the full expected
snowflake/cffi complaint text rather than a bare package-name prefix
(P3). **Status: Fixed.** Operator-verified 2026-07-18: `docker compose build`
succeeded against a live `.env` on Windows Docker Desktop 29.6.1, and
`airflow-scheduler`/`airflow-webserver` came up healthy from the built image
(see `docs/runbooks/01a_compose_paper_runtime_verification.md`).

### BUG-003: Paper-trading artifacts are written to an unmounted container path

**Severity:** P0 / approval workflow blocker

**Evidence:** The paper DAG defaults artifacts to `/opt/airflow/rqis_paper` and documents it as a shared Docker Compose volume. Compose does not mount that path into Airflow containers or the dashboard.

**Impact:** Blotter artifacts can be invisible to host-side approval tools/dashboards and may disappear on container recreation.

**Suggested direction:** Add a named volume or host bind mount for `RQIS_PAPER_ARTIFACT_DIR` shared by Airflow and the dashboard/approval tooling.

**Resolution (Gate 01A, branch `dev/R2-01A-compose-runtime`):** confirmed
there is no Compose dashboard service (the Streamlit dashboard is launched
host-side via `streamlit run reporting/dashboards/app.py`), so
`docker-compose.yml` now bind-mounts
`${RQIS_PAPER_ARTIFACT_HOST_DIR:-./local/paper_artifacts}` at the identical
in-container path `/opt/airflow/rqis_paper` on every Airflow service, and
`.env.example` documents the corresponding host-side
`RQIS_PAPER_ARTIFACT_DIR` value the dashboard process should export. The
`airflow-init` startup command now creates the directory and performs a
write-permission check before `airflow db migrate`.
`tests/infra/test_paper_artifact_shared_storage.py` proves a container
write (as uid 50000, the base image's non-root `airflow` user) is
host-readable with a matching SHA-256, and that all three Airflow services
mount the same path from the same env-configurable source. Adversarial fix
round (same branch): the Docker smoke test's mount list is now derived from
`docker-compose.yml` `x-airflow-common.volumes` instead of being duplicated
by hand, with a consistency test that fails if a compose mount the DAG
runtime needs is removed (P2-3). **Status: Fixed.** Operator-verified
2026-07-18: a sentinel written from inside `airflow-scheduler` to
`$RQIS_PAPER_ARTIFACT_DIR` was read back byte-identical from the host bind
mount both before and after an `airflow-scheduler` container restart.

### BUG-004: IBKR connectivity defaults to container-local localhost

**Severity:** P0 / broker connectivity blocker

**Evidence:** `IBKRBroker` defaults to `IBKR_HOST=127.0.0.1`; `.env.example` does the same; the DAG constructs `IBKRBroker()` without a host. In Docker, `127.0.0.1` is the container, not the host running TWS/IB Gateway.

**Impact:** Even with env vars passed, containerized paper trading will usually fail to reach the broker socket.

**Suggested direction:** Document and configure a Docker-safe host (`host.docker.internal` plus Linux `extra_hosts`, host networking, or explicit gateway IP), and require a connectivity preflight in containerized runs.

**Resolution (Gate 01A, branch `dev/R2-01A-compose-runtime`):** added a
distinct `IBKR_HOST_AIRFLOW` `.env` variable (default `host.docker.internal`)
feeding the Airflow containers' `IBKR_HOST`, kept separate from the
host-side `IBKR_HOST=127.0.0.1` used by operator CLI scripts, plus a
portable `extra_hosts: ["host.docker.internal:host-gateway"]` mapping for
Linux Docker Engine compatibility (Windows Docker Desktop targeted now per
the plan's open decision 3; Linux untested). More importantly, added a
production (not test-only) fail-closed guard,
`execution/brokers/ibkr.py._validate_bridged_broker_host()`, called from
both `IBKRBroker.__init__` and `connect()`: when
`RQIS_RUNTIME_CONTEXT=compose_bridged` is set (every Airflow Compose
service sets it), an unset/empty/`localhost`/`127.0.0.1`/`::1`/`0.0.0.0`
`IBKR_HOST` raises `OSError` before any connection attempt, with a declared
`RQIS_RUNTIME_NETWORK_MODE=host` escape hatch for a genuinely
host-networked deployment. `execution/tests/test_ibkr_bridged_host_validation.py`
and `execution/tests/test_ibkr_broker_endpoint_fail_closed.py` cover
unset/empty/loopback rejection, the live-port-7496 failure path, and an
unresolvable-host `connect()` failure propagating out of the DAG's
`_fetch_ibkr_snapshot` task (the first broker-touching task) without being
swallowed. Adversarial fix round (same branch): the guard now treats ANY
non-empty `RQIS_RUNTIME_CONTEXT` value as containerized fail-closed (a typo
like `compose-bridged` can no longer silently deactivate enforcement —
P2-2), and the DAG's `_require_paper_env` requires the marker outright so
the guard cannot be bypassed by omitting it (P1-1).
**Status: Fixed.** Operator-verified 2026-07-18: `python -m
scripts.paper_readiness_check` run from inside `airflow-scheduler` reported
`OK: TWS/Gateway socket reachable at host.docker.internal:7497` and `OK:
IBKRBroker connected in paper mode` against a live TWS paper session,
proving the container correctly reaches the host broker through
`host.docker.internal` rather than falling back to a container-local
address. (The script then failed a downstream, unrelated CAD/USD FX-rate
fetch because the paper account lacks a live IBKR FX market-data
subscription — a pre-existing account limitation with an existing
`IBKR_FX_RATE_CAD_USD`/`_AS_OF` manual fallback documented in
`CLAUDE.md`/`.env.example`, not a BUG-004 defect.)

### BUG-005: Approval quantity overrides can be tampered upward and bypass validation

**Severity:** Critical / trading safety

**Evidence:** The dashboard caps operator quantity edits, but Airflow later trusts `quantity_overrides` from the approval row/XCom and applies them directly. The quantity validator checks `estimated_shares`, while order construction submits `quantity` when present.

**Impact:** A malicious or erroneous DB row such as `quantity_overrides={"1": 1000000}` can cause an oversized broker order to be submitted while the existing validator still passes on original `estimated_shares`.

**Suggested direction:** Validate each override server-side immediately before submission: integer, finite, positive, less than or equal to the original approved share count, selected order only, and re-run notional/risk checks on the overridden rows.

### BUG-006: Corrupt partial reconciliation artifacts fail open and can duplicate broker orders

**Severity:** Critical / trading safety

**Evidence:** On retry, `_submit_orders` attempts to read an existing reconciliation artifact to skip previously submitted sequence IDs. If the artifact exists but is corrupt/unreadable, the code logs and continues with an empty `already_submitted_seqs`; later it can also reset `previous_responses` to `[]`.

**Impact:** A partial write, truncation, or manual edit can cause the retry path to resubmit orders already accepted by IBKR.

**Suggested direction:** Fail closed on corrupt reconciliation artifacts and require manual broker reconciliation or broker-backed idempotency proof before retrying any submission.

### BUG-007: Risk dashboard can silently report zero/incorrect risk due to position schema mismatch

**Severity:** P0 / risk monitoring correctness

**Evidence:** The paper DAG writes positions with keys including `price`; the Risk page builds its price map from `current_price` only. When positions only have `price`, they are excluded from risk weights. The risk monitor then treats empty weights as zero concentration/beta exposure rather than a hard data-quality failure.

**Impact:** A real portfolio can appear to have no concentration/beta risk, and circuit-breaker display/alerts may not reflect actual holdings.

**Suggested direction:** Standardize the portfolio snapshot schema, accept the producer’s price field or migrate producers/consumers together, and fail closed when positions cannot be priced.

### BUG-008: Current-membership universe creates survivorship/data leakage in IC research

**Severity:** P0 / research validity

**Status:** Fixed. Merged to `dev/R2-phase1` via PR #34 (`1df242e`), branch
`dev/R2-01B2-pit-universe` (roadmap item 01B-2, scoped to
`docs/plans/01b-research-validity-design.md` §1). Review history: one internal
adversarial round plus six Codex review rounds (seven items total, including
two P1s beyond the adversarial round's own findings — SQLAlchemy 1.4
compatibility for the DAG's PIT filter, and PIT membership applied before
cross-sectional z-scoring, closed as a class sweep across all four factor
composites — and a final P2 on the quality factor's independent fundamentals
load bypassing that same eligibility filter). 1608+ tests passing at merge.
Delivered: Alembic migration 009 (effective-dated `universe_membership`,
`universe_symbol_history`, `universe_import_batches`); provider-agnostic import
pipeline with checksummed raw-source persistence and publish-only-validated gates
(`data/universe/import_pipeline.py`); real Wikipedia constituent history imported
and verified from the checked-in snapshot `data/vendor/wikipedia_sp500/2026-07-17/`
(source contract: `docs/plans/01b2-constituent-source-contract.md`); fail-closed
runtime API `data/universe/runtime.py` (`load_universe_as_of` / `PITUniverseLookup`)
with type-level rejection of current-universe objects in historical code;
`compute_ic_series`, `scripts/validate_signal_ic.py`, and
`scripts/backfill_momentum_scores.py` migrated to PIT membership enforcement;
operational current-mode callers explicitly labeled. All §1.4 acceptance tests
pass (`data/tests/universe/test_acceptance_1_4.py`). Historical outputs computed
under the old current-membership universe remain provisional; recompute is 01B-3/§4
scope. Residual data-quality limits tracked as BUG-068/BUG-069.

Adversarial-review fix round (same branch): operator `--exclude-tickers`
exclusions are now persisted as a JSON audit record on
`universe_import_batches.excluded_tickers` (migration 009 amended pre-release)
and surfaced by `coverage_report()`; migration 010 adds an interim
`signal_ic_stats.provisional` boolean (existing rows default TRUE — all
pre-01B-2 rows are provisional by definition; superseded by 01B-3's §4
`research_run_id` identity) stamped by `scripts/validate_signal_ic.py`;
the Wikipedia changes-table parser validates actual header text before its
positional rename and fails closed on reordered/renamed headers; left-censored
intervals get `known_at` at the coverage-start session close so pre-window
members are eligible on day one of the certified window; the survivorship
warning in `validate_signal_ic.py` is gated to provisional runs; snapshot
checksum-tamper detection is now covered by tests.

**Evidence:** The research universe is documented as current-membership S&P 500 and excludes removed constituents. The IC engine merges scores to forward returns on available ticker/date rows without enforcing point-in-time membership.

**Impact:** Historical IC can be biased upward by excluding bankrupt, removed, or underperforming names that were in the investable universe at the time.

**Suggested direction:** Add a PIT constituent/membership table and require IC computations to filter by membership as of each signal date.

### BUG-009: Same-close signal/return timing can introduce lookahead

**Severity:** P0 / research and backtest validity

**Evidence:** Forward returns are computed from signal-date close to future close. Many price signals use the signal-date close in the signal itself.

**Impact:** Unless the project explicitly assumes known-before-close signals and close/auction execution, IC/backtests can include a one-bar lookahead.

**Suggested direction:** Shift signals or forward-return windows to the next executable bar, or enforce timestamped market-on-close assumptions.

**Status:** Fixed. Merged to `dev/R2-phase1` via PR #35 (`71e6636`), branch
`dev/R2-01B3-timing-contract` (roadmap item 01B-3, scoped to
`docs/plans/01b-research-validity-design.md` §2 and §4). Review history: one
internal adversarial round plus 11 Codex review rounds — every P0/P1 finding
resolved and verified before merge, including a genuine PIT lookahead leak in
the realized-return series (round 3/critical) and a consolidated
methodology-honesty enforcement point replacing four ad hoc per-site checks
(round 11). One P2 finding (BUG-074) arrived 8 minutes before merge and was
not triaged before the operator merged; filed as a follow-up. Delivered: `signals/research/timing.py`
(`TimingPolicy`, `build_return_series`, `reject_same_date`) enforcing the
baseline `score_date < entry_date < exit_date` (t+1 close) convention on each
ticker's own trading calendar; `signals/research/ic.py`
`compute_forward_returns`/`compute_ic_series` rewritten to delegate to it,
name every date explicitly (`score_date`/`entry_date`/`exit_date` — never a
bare `date`), and check PIT membership on all three dates, not just
`score_date`; two explicitly named corporate-action interfaces in
`data/normalization/corporate_actions.py`
(`build_score_price_history_as_of`, `build_realized_total_return_as_of`)
backed by a new `known_at`/`announced_at`/`source_version` availability
contract on `corporate_actions` (migration 011, conservative next-session
backfill for legacy yfinance rows); versioned research identity
(`research_methodologies`/`research_runs`, migration 012) with a
`research_run_id` FK now part of the unique constraint/primary key on
`signal_ic_stats`/`factor_scores`/`alpha_scores` so a new run cannot silently
UPSERT over an old methodology's rows; `scripts/validate_signal_ic.py` and
`scripts/audit_pit_safety.py` updated accordingly (the audit script now also
empirically verifies entry/exit alignment on live price data, not just
`score_date < sim_date`).

**Adversarial review fix round (same day):** the first pass wired the
timing-contract fix into `signals/research/ic.py` but left the two new
cutoff-aware corporate-action builders with no production caller — the live
`scripts/validate_signal_ic.py`/`scripts/backfill_momentum_scores.py` still
loaded raw `close` with zero adjustment (worse than the pre-existing
full-history routine). Both scripts now call `build_score_price_history_as_of`
(momentum/lowvol — price-ratio-based factors) and
`build_realized_total_return_as_of` (the forward/realized-return leg for
every factor) before prices reach scoring/`compute_ic_series` — see BUG-071
for the documented residual (single run-boundary cutoff, not literal
per-score-date). Also fixed: `airflow/dags/daily_signal_pipeline.py` would
have hard-failed its first post-migration run (no active research run was
ever registered for the methodology name it requires) — see
`scripts/register_operational_research_run.py` and the pre-deploy blocker
note below. `data/normalization/corporate_actions.py`'s cutoff filter now
also requires an action to have occurred (`ex_date <= cutoff`), not just been
announced, matching `build_realized_total_return_as_of`'s docstring.

Full non-tearsheet repo suite passing at review time (1600+ tests). No
historical scores/backtests were recomputed by this change (identity/
invalidation machinery only, per the design plan's implementation order).

**PRE-MERGE/PRE-DEPLOY BLOCKER:** before migration 012 is applied to any
database `daily_signal_pipeline.py` runs against, an operator MUST run
`python -m scripts.register_operational_research_run` once (idempotent) to
register and activate the `daily_signal_pipeline_operational` methodology/
run — otherwise the next scheduled DAG run (`30 21 * * 1-5`) fails closed
with `NoActiveResearchRunError`. See
`docs/runbooks/research_run_registration.md`.

**Adversarial review round 2 (same day):** two more confirmed findings.
`airflow/dags/daily_signal_pipeline.py` never got the corporate-action
adjustment wiring the CLI scripts received — `_load_prices` now also loads
`corporate_actions` and calls `build_score_price_history_as_of` with
`score_cutoff = session_close_cutoff(end_date)` (this DAG scores exactly one
date per run, so this is the exact per-date cutoff, no BUG-071-style
approximation) before `_compute_momentum`/`_compute_lowvol` run; a new
`adjusted_prices_json` XCom key carries the adjusted series,
`_compute_value`/`_compute_quality` keep the raw `prices_json` key. Fixing
this surfaced a second, unrelated latent bug: `_write_scores`'s active-run
lookup imported `data.research.identity`, which imports SQLAlchemy-2-only
ORM APIs the packaged Airflow image (SQLAlchemy 1.4.51 pinned, see
`infra/docker/Dockerfile.airflow`) cannot import — replaced with a plain-SQL
`_get_active_research_run_id_sql`, kept in semantic lockstep via new parity
tests, and the DAG's existing import-isolation test now bans that import
path. Also fixed: migration 011's `sys.path` shim resolved to `infra/`, not
the repo root (`parents[3]` → `parents[4]`, with an assertion guard).

**Adversarial review round 3 (same day):** research_run_id is now part of
`alpha_scores`/`factor_scores` identity, so multiple rows can legitimately
coexist for the same `(ticker, score_date, strategy_id)` across runs
(legacy, superseded, active) — every production reader must filter to the
active run explicitly, never read across all of them. Fixed:
`scripts/paper_inputs_check.py::_load_latest_scores` (and, through it,
`paper_target_check.py`/`paper_order_candidates_check.py`/
`paper_risk_compliance_check.py`/`paper_stage_blotter_check.py`, all of
which import it) now resolves and filters to the active
`daily_signal_pipeline_operational` run, failing closed with an actionable
message if none is active; `daily_signal_pipeline.py`'s `_write_simulation`
fallback query (for strategies not present in the current run's own XCom)
now filters the same way, degrading (skip, not crash) if no active run can
be resolved; `scripts/pin_snapshot.py` gained a `--research-run-id` option
and detects+rejects (rather than silently pinning) the case where more than
one run's rows collide on the same `(ticker, score_date)`. Also fixed (P2):
`scripts/backfill_momentum_scores.py` raised a bare `KeyError` against a
`corporate_actions` snapshot pinned before migration 011 (no
`known_at`/`source_version` columns); it now synthesizes `known_at` via the
same conservative next-session rule migration 011 used, tagging synthesized
rows with a distinct `source_version` so they remain distinguishable from a
genuinely migrated live-table read. See BUG-072 for the one reader class
(Streamlit dashboard queries, `scripts/indicator_diagnostic.py`) explicitly
scoped OUT of this round with rationale, rather than silently left
unaddressed.

**Adversarial review round 4 (same day) — the most serious finding of this
whole review series:** the single-run-boundary-cutoff simplification
documented as BUG-071 was safe for the SCORE series but NOT for the
REALIZED-RETURN series: for an earlier exit date in a holdout, an action
whose `ex_date` fell between that specific entry/exit pair but whose
`known_at` was after that exit (yet before the later shared boundary) was
incorrectly included — future information leaking into a persisted,
"PIT-safe" IC result, for every exit date in a holdout except the very
last one. This is fixed, not approximated: new
`signals.research.ic.compute_realized_forward_returns_as_of` builds a
genuinely per-exit-date-cutoff-correct realized-return series (one
`build_realized_total_return_as_of` call per distinct exit date needed,
not one shared boundary), and `compute_ic_series` gained a
`precomputed_forward_returns` parameter so `scripts/validate_signal_ic.py`
can feed it in directly, bypassing the same-series shortcut entirely for
the realized-return leg. `_build_adjusted_price_series` was split into
`_build_score_adjusted_prices` (score leg only, single boundary cutoff —
re-verified safe for a structurally different reason than the
realized-return case, see BUG-071's updated entry) plus the new realized-
return path. A new test (`TestComputeRealizedForwardReturnsAsOf::
test_action_unknown_at_earlier_exit_does_not_adjust_it`) reproduces the
exact leak scenario and proves the earlier exit's return is unaffected by
an action not yet known at ITS cutoff, while a later exit whose own cutoff
does cover it is adjusted correctly.

Also fixed the same round: `scripts/backfill_momentum_scores.py`'s
`--research-run-id` CLI flag is no longer `required=True` (it blocked the
documented `--dry-run` preview command, which never persists and never
needed it — `run()` still hard-requires it for an actual write); and
`reporting/dashboards/queries.py`'s `latest_alpha_scores`/
`bottom_alpha_scores`/`factor_scores_for_ticker` now filter to the active
research run (closing the `queries.py` portion of BUG-072 — see that
entry for what remains open there).

**Adversarial review round 5 (same day):** the round-2 fix (plain-SQL
active-run lookup, avoiding the SQLAlchemy-2-only ORM in
`data.research.identity`/`data.research.models` because the packaged
Airflow image pins SQLAlchemy 1.4.51) was reintroduced as a regression at a
SECOND call site: round 3's `scripts/paper_inputs_check.py::
_resolve_active_research_run_id` fix used the ORM `get_active_research_run`
directly, and `paper_inputs_check.py` is Airflow-reachable via
`airflow/dags/daily_paper_trading.py`'s `_verify_inputs`/`_construct_target`
tasks — the identical failure mode as round 2, just a different file. Fixed
by extracting the lookup into a new shared module,
`data/research/sql_compat.py` (plain `text()` SQL, zero ORM imports),
that both `airflow/dags/daily_signal_pipeline.py::
_get_active_research_run_id_sql` and `scripts/paper_inputs_check.py::
_resolve_active_research_run_id` now delegate to, so the lookup exists in
exactly one place instead of being reimplemented (or regressed) at each
new call site. Repo-wide grep confirmed no other Airflow-reachable call
site had the same regression. Added
`tests/test_airflow_sqlalchemy_import_isolation.py`: a generic,
repo-wide (not DAG-specific) static check that walks every DAG under
`airflow/dags/` for `scripts.*` imports (module-level or lazy, anywhere in
the file) and asserts neither the DAG nor any transitively-imported script
imports the banned SQLAlchemy-2-only modules — so this class of bug fails
a fast local test instead of recurring silently a third time.

Also fixed (P2): `compute_ic_series`'s `precomputed_forward_returns` mode
previously stamped output rows with the function's own `timing_policy`
argument rather than the `timing_policy_id` actually present in the
supplied frame's rows — mislabeling the persisted research identity for
any caller building a frame under a non-default policy, or silently
picking one policy if the frame mixed several. Now reads the frame's own
column (rejecting with a clear error if it contains more than one distinct
value) instead of trusting the argument.

**Adversarial review round 6 (same day):** `reporting/dashboards/
queries.py::pipeline_health()`'s signals-recency check ran
`MAX(computed_at)` over ALL of `alpha_scores` with no active-run filter —
the one dashboard query round 4's `_ACTIVE_RUN_SUBQUERY` fix missed. A
fresher row left behind by an inactive/superseded run (post-backfill or
run rotation) could make the dashboard report the pipeline healthy while
the active `daily_signal_pipeline_operational` run was actually stale or
had never written anything, undermining the active-run invariant this PR
spent five rounds establishing everywhere else. Fixed with the same
`_ACTIVE_RUN_SUBQUERY` pattern already used by the other three dashboard
queries in this file (kept for in-module consistency rather than switching
to `data/research/sql_compat.py`, which raises on no-active-run rather
than degrading to an empty/None result the way this file's existing
pattern does). New `tests/reporting/dashboards/test_pipeline_health.py`
(the function had zero prior test coverage) reproduces the exact scenario
Codex described and proves the active run's own staleness now wins over a
fresher inactive run's row. Also hardened `_age()` to accept a string
timestamp defensively (a SQLite/pysqlite `MAX()`-over-aggregate quirk that
loses the declared column type, surfaced only by adding real test
coverage — the production Postgres driver was never affected).

**Adversarial review round 7 (same day):** three P2 findings, closing out
this review series. (1) The remaining two dashboard readers,
`reporting/dashboards/queries.py`'s `alpha_score_at_fill_date`/
`factor_scores_at_fill_date` and `reporting/dashboards/simulation.py`'s
`alpha_overlap_matrix`, were fixed with the same `_ACTIVE_RUN_SUBQUERY`
pattern (see BUG-072, now closed for every reader except the deliberately
deferred `scripts/indicator_diagnostic.py`); a repo-wide grep for every
remaining `FROM alpha_scores`/`FROM factor_scores` confirmed nothing else
is unfiltered without a documented reason. (2)
`scripts/register_operational_research_run.py` registered
`action_source_version="yfinance-current"`, but ingestion
(`data/ingestion/market/yfinance_client.py`,
`airflow/dags/daily_data_pipeline.py`) calls
`TimescaleWriter.upsert_corporate_actions(df)` without a `source_version`
argument, so every row actually lands with the writer's own default,
`"unknown"` — the registered methodology's provenance claim didn't match
reality, undermining exactly what migrations 011/012 exist to guarantee.
Fixed by registering `"unknown"` (matching actual behavior) rather than
wiring a real version through ingestion (out of scope for this fix — that
touches production ingestion code, not just methodology metadata); the
`notes` field now documents the gap and the tightening path (re-register a
NEW methodology once ingestion passes a real version, never edit this one
in place). (3) `signals.research.ic.compute_realized_forward_returns_as_of`
(the round-4 correctness fix) rebuilt a full adjusted price panel per
DISTINCT EXIT DATE — assessed as a real but cheaply fixable performance
concern, not deferred: it now caches the expensive panel build keyed by
the actual SET of eligible corporate actions for a cutoff (reusing
`_filter_actions_by_cutoff` directly so the cache key can never drift from
what the builder itself considers eligible), since many consecutive exit
dates in a multi-year holdout (~500-750 distinct trading dates) commonly
share an identical eligible-action set — nothing new becomes knowable
between them — collapsing what was an O(#exit_dates) loop of full-panel
rebuilds to O(#distinct eligible-action-set changes), which is bounded by
the number of corporate actions (typically dozens, far fewer than trading
dates) rather than the holdout length. This is a pure performance
optimization — the cache key is exactly the input that determines the
adjusted panel's content, so it cannot change correctness — proven by new
tests (`test_consecutive_exit_dates_with_no_new_actions_share_one_panel_build`,
`test_new_action_forces_a_fresh_panel_build`) that count the underlying
builder calls directly, plus the existing round-4 leak-reproduction test
continuing to pass unchanged.

**Adversarial review round 8 (same day):** one P1, one P2. (1) **P1:**
`scripts/register_operational_research_run.py`'s `ensure_operational_run()`
returned the live `ResearchMethodology` ORM instance, but `main()` read
`methodology.id`/`.name` from it AFTER the `with Session(engine) as
session:` block had already closed the session. Because
`session.commit()` expires ORM instances by default
(`expire_on_commit=True`), that post-close attribute access re-queried
through a closed session and raised `DetachedInstanceError` —
`main()`'s very first successful run would crash on its own final print
statement, the exact "P1-fix-for-a-P1-fix" scenario this script exists to
prevent (this script was built in round 3 specifically so
`daily_signal_pipeline.py` doesn't fail closed on its first post-migration-
012 run). Confirmed by running the actual script end-to-end against a real
(SQLite) engine, not just the mocked-session unit tests, which had kept the
session open across every assertion and so never exercised the close
boundary that trips this bug. Fixed: `ensure_operational_run()` now reads
`.id`/`.name` while the session is still open and returns plain scalars
(`methodology_id: int, methodology_name: str, run_id: int, created: bool`)
instead of the ORM object — the caller can no longer touch an
expired/detached instance because it never receives one. New regression
test `test_result_survives_session_close_like_main_does` in
`tests/test_register_operational_research_run.py` deliberately mirrors
`main()`'s exact open-then-close pattern (unpacks the result only after the
`with Session(...)` block exits) rather than asserting inside it, so it
would have caught this before it shipped. (2) **P2, closing the last open
item of BUG-072:** `scripts/indicator_diagnostic.py::_load_factor_scores`
loaded `factor_scores` by `strategy_id`/date range only, with no
`research_run_id` filter — round 7 had left this open as a "deliberate,
documented" research/diagnostic-tool exception, but Codex made the fair
point that migration 012's widened PK (adding `research_run_id`) makes this
a real correctness risk for the tool's own stated purpose, not just
staleness: the tool's own duplicate-row detector only warns, and
`pivot_table` silently averages duplicate `(ticker, score_date,
factor_name)` rows, so a mixed-run blend could reach the reliability/
validity report undetected. Fixed by defaulting to the same
`_ACTIVE_RUN_SUBQUERY` pattern used everywhere else under BUG-072, with a
new `--all-runs` explicit opt-in that itself fails closed (raises) on a
real cross-run collision rather than silently blending. See BUG-072 for the
full writeup — that entry is now fully closed.

**Adversarial review round 9 (same day):** one P1, one P2. (1) **P1:**
`scripts/backfill_momentum_scores.py` silently degraded to raw (unadjusted)
prices when no `corporate_actions` snapshot was pinned for
`--snapshot-date`, and LIVE (non-dry-run) writes proceeded anyway —
persisting momentum scores tagged with a `research_run_id` whose
registered methodology (`score_cutoff_known_at_v1`) falsely claims
cutoff-adjusted corporate-action handling was applied. The same
"provenance lies about what actually happened" pattern as the original P0
finding this whole task exists to prevent, reached this time via a silent
degrade instead of missing wiring. Fixed: a live write now fails closed by
default on a missing snapshot; `--dry-run` stays permissive (preview only,
never persists — no persisted provenance to lie about). A new
`--allow-raw-prices-on-missing-actions` opt-in requires `--research-run-id`
and a new `_validate_raw_prices_methodology_is_honest` helper queries that
run's registered methodology, refusing to proceed if
`score_action_availability_policy == "score_cutoff_known_at_v1"` — the
operator must register and pass a run under a methodology that honestly
declares no cutoff adjustment was applied. (This script is not
Airflow-reachable, so using the ORM here is safe, unlike the DAG-reachable
modules elsewhere in this bug.) New tests
(`data/tests/universe/test_backfill_universe_filter.py::
TestMissingActionsFailsClosedOnLiveWrite`,
`TestValidateRawPricesMethodologyIsHonest`) cover: live write without
opt-in raises before any DB write, opt-in without `--research-run-id`
raises, opt-in with a dishonest methodology raises, dry-run stays
permissive, and the honesty gate is unit tested directly. (2) **P2:** the
`precomputed_forward_returns` bypass in `compute_ic_series`
(`signals/research/ic.py`, added round 4) validated columns before use but
never checked `score_date < entry_date < exit_date` on the supplied rows
— it deliberately skips the internal `build_return_series` call where
that invariant is normally enforced, making it the one entry point that
could silently reintroduce the exact BUG-009 lookahead. Fixed: new
`_validate_return_series_date_ordering` checks every distinct date triple
in the frame (deduped across tickers sharing a cross-section), reusing
`signals.research.timing.reject_same_date` for the score/entry pair and
raising the same `SameDateScoreError` for entry/exit. Both entry points to
`compute_ic_series` now enforce the identical invariant. New tests in
`signals/tests/test_ic.py` reproduce a same-close precomputed frame being
rejected, an exit-before-entry frame being rejected, and confirm a
genuinely valid hand-built frame still passes.

The PM independently reproduced and confirmed round 8's BUG-073 testpaths
finding this round (old buggy config: 1685 vs corrected 2096, previously
hidden subtrees run clean) — no further action needed there.

**Adversarial review round 10 (same day):** two P2s, plus a required
self-audit sweep before closing the round. (1) **P2:**
`scripts/audit_pit_safety.py::_empirical_audit` recomputed expected
momentum from raw `daily_prices.close` with no corporate-action
adjustment, but the backfill (round 4/9) writes scores from a
cutoff-adjusted `adj_close` series — any audited window crossing a
split/dividend showed a spurious mismatch, making the audit tool itself
unreliable exactly where it matters most. Fixed: new
`_adjusted_prices_for_audit` rebuilds the same cutoff-adjusted scoring
input the backfill actually wrote scores from (reusing
`build_score_price_history_as_of`), with an optional
`--corporate-actions-file`/automatic snapshot load that degrades to a
loud raw-price caveat (not a failure — this is a read-only diagnostic
tool) if unavailable. New test
`test_audit_empirical_false_positives_without_corporate_actions_but_clean_with_them`
reproduces the exact finding: raw recomputation false-positives across a
split window; supplying `corporate_actions` reports zero violations. (2)
**P2:** `--provisional-no-universe` on `scripts/backfill_momentum_scores.py`
could be combined with a live write tagged to the ACTIVE operational
`research_run_id` (whose methodology claims PIT-universe safety), letting
current-membership (survivorship-biased) rows masquerade as PIT-safe to
any downstream reader filtering solely by `research_run_id` (BUG-072) —
same honesty-check pattern as round 9, on the universe dimension instead
of the corporate-action dimension. Fixed: new
`_validate_provisional_no_universe_methodology_is_honest` (sharing the
plain-scalar `_get_methodology_fields_for_run` lookup with round 9's
check) refuses a live `--provisional-no-universe` write unless
`--research-run-id` points at a methodology that honestly declares no PIT
filtering was applied. New tests in
`data/tests/universe/test_backfill_universe_filter.py::
TestProvisionalNoUniverseFailsClosedOnLiveWrite`/
`TestValidateProvisionalNoUniverseMethodologyIsHonest` cover the gate and
its unit-level honesty check directly; the round-9 tests that had used
`--provisional-no-universe` merely as a convenience were updated to use
the PIT `lookup` fixture instead, since the new round-10 gate would
otherwise intercept them before they reached what they were actually
testing.

**Round-10 self-audit sweep (required before closing the round):** a
comprehensive repo-wide check across four dimensions, per explicit review
directive.
- *Corporate-action adjustment wiring*: grepped every production path
  reading `daily_prices`/computing a return. Found and fixed a THIRD
  instance of the same defect class:
  `airflow/dags/daily_signal_pipeline.py::_write_simulation`'s
  close-to-close daily return divided RAW prices with no adjustment — a
  split/dividend on `sim_date` or `prev_date` would inject a fabricated
  return into the COMPOUNDING `simulated_nav` chain, permanently
  distorting every later NAV row for the strategy. Fixed via new
  `_adjusted_closes_for_simulation`, reusing
  `build_realized_total_return_as_of` with `entry_date=prev_date` and
  `exit_cutoff=session_close_cutoff(sim_date)` (exact per-run cutoff, not
  an approximation, since this task scores exactly one `sim_date` per DAG
  run); non-blocking degrade-to-raw on any adjustment failure
  (`strategy_simulations` has no `research_run_id` column, so there is no
  provenance-honesty claim to violate by degrading). Extracted as a
  standalone testable function since the production INSERT uses
  Postgres-only `jsonb` CAST syntax SQLite tests can't exercise
  end-to-end. New tests in `tests/test_daily_signal_pipeline_pit.py::
  TestSimulationCorporateActionAdjustment` prove a split is correctly
  back-adjusted (raw closes would imply a fake ~49% loss; adjusted is a
  genuine +2% gain), and that missing/absent corporate actions degrade to
  raw without raising. `signals/composites/momentum_score.py`/
  `low_vol_score.py` are pure functions operating on whatever series their
  caller passes and were confirmed to always be fed already-adjusted
  series by every caller checked.
- *SQLAlchemy 1.4/2.0 isolation test robustness*: audited
  `tests/test_airflow_sqlalchemy_import_isolation.py` itself and found it
  only walked ONE level deep (the DAG plus its direct `scripts.*`
  imports), not the full transitive closure — a script reachable ONLY
  through another script (a real, multi-level import graph exists in the
  `scripts/paper_*_check.py` family) would have been silently unchecked,
  and `data.*` modules reached only through a script were never checked at
  all. Rewritten to a proper BFS closure over every `scripts.*`/`data.*`
  module transitively reachable from each DAG, with a sanity assertion
  that the BFS actually reaches depth > 1 for `daily_paper_trading.py` (so
  a future regression narrowing this back to depth-1 fails the test rather
  than silently passing). No live gap found — the repo's actual import
  graph is currently clean — but the coverage gap in the guard itself was
  real.
- *Provenance-honesty sweep*: grepped every `research_run_id=`/
  `research_run_id[...]=` write-site repo-wide. Found and fixed a second
  instance of the round-10 universe-honesty gap:
  `scripts/validate_signal_ic.py`'s `--persist` path (writes
  `signal_ic_stats`) had the identical `--provisional-no-universe` +
  `--research-run-id` risk as `backfill_momentum_scores.py` — its per-row
  `provisional` column (migration 010) self-describes honestly, but
  migration 012's docstring is explicit that `research_runs.status`/
  `is_active` (reached via `research_run_id`), not `provisional`, is now
  the authoritative marker, so a reader trusting that newer model could
  still be misled by a PIT-claiming methodology tag. Fixed with the same
  `_validate_provisional_no_universe_methodology_is_honest` pattern
  (duplicated deliberately rather than refactored into a shared module,
  to avoid touching already-reviewed round-9/round-10 code under review
  pressure), wired into `main()` alongside the existing
  `--persist`/`--research-run-id` requirement check. `airflow/dags/
  daily_signal_pipeline.py::_write_scores` and `scripts/pin_snapshot.py`
  were reviewed and found exempt by design (the former resolves
  `research_run_id` via the DAG's own fixed active-run lookup with no
  operator-suppliable override; the latter already has its own explicit
  `--research-run-id` opt-in plus same-natural-key collision detection,
  round 3). `data/storage/timescale_writer.py`'s `upsert_factor_scores`/
  `upsert_alpha_scores` are infra-layer writers that require
  `research_run_id` present but have no methodology-claim semantics of
  their own — the honesty check is each higher-level caller's
  responsibility, already covered at every current call site.
- *Active-run read-filtering final sweep*: repeated the BUG-072 grep for
  every `FROM alpha_scores`/`FROM factor_scores`/ORM-table-reflection
  read across the full repo (including `notebooks/`, `mcp_servers/`,
  which currently have no matching code). Confirmed every reader is
  either filtered via `_ACTIVE_RUN_SUBQUERY`/an explicit `research_run_id`
  parameter, or is one of the two documented exceptions
  (`scripts/pin_snapshot.py`, `scripts/indicator_diagnostic.py`'s
  `--all-runs` opt-in). No new gap found.

### BUG-010: `pct_change()` missing-data defaults distort many indicators

**Severity:** P0 / signal correctness

**Status:** Fixed. Merged to `dev/R2-phase1` via PR #32 (`65e1b72`), branch
`dev/R2-01B1-missing-data` (roadmap item 01B-1, scoped to
`docs/plans/01b-research-validity-design.md` §3).

**Evidence:** Multiple indicators call `pct_change()` without `fill_method=None` on wide data containing NaNs.

**Impact:** Pandas can forward-fill missing prices before return calculations, creating artificial zero returns, suppressed volatility/beta, and distorted volume-price signs.

**Suggested direction:** Use `pct_change(fill_method=None)` consistently and require sufficient non-null observations per ticker/window.

**Fix summary:** Inventoried and migrated all 33 production `pct_change()` call sites
(`docs/plans/01b1-pct-change-inventory.md`) across `signals/indicators/*` (momentum,
volume, volatility), `backtesting/engine/{data_handler,event_loop}.py`,
`portfolio/risk_model/covariance.py`, and `reporting/dashboards/{queries.py,
pages/5_Performance.py}`. Added `signals.indicators._price_utils.daily_return`
(`pct_change(fill_method=None)` + positive-finite price validation),
`rolling_valid_count`/`require_full_window` for cumsum/mask-based indicators that
don't propagate NaN through arithmetic (`volume_up_down_ratio_21d`,
`obv_momentum_21d/63d`, `price_volume_trend_21d`), and raised every return-derived
rolling `min_periods` to its full window so a gap suppresses the value by default
(one documented exception: `vol_trend_slope_63d`'s outer OLS trend fit, which keeps
its existing internal robust-minimum tolerance). Added
`tests/test_pct_change_guard.py`, a repo-wide regression guard that fails on any new
unguarded `pct_change()` call in a production price-return path. 768 signals tests,
218 backtesting tests, 30 portfolio tests, and 100 reporting/dashboard tests pass.

**Adversarial-review fix round:** the same fabrication class survives in indicators
that never call `pct_change()`. Fixed: RSI family (`rsi_14`, `rsi_14_raw`, `rsi_28`,
`stoch_rsi_14`) and `ease_of_movement_14d`/`force_index_13d`, where EWM with pandas'
default `ignore_na=False` decays through a missing session's diff/flow input and
emits a frozen duplicate value on/after a gap — now gated with `require_full_window`
over the estimator's nominal span; `ad_line_momentum_21d` and `chaikin_oscillator`
(ungated `cumsum` flow, identical to the OBV/PVT defect) — now gated on the trailing
flow window; `money_flow_index_14d` (`.where(tp_change > 0, 0.0)` fabricates a zero
flow on gap days) — gated on trailing `tp_change` validity; `chaikin_money_flow_21d`
`min_periods` raised to full window; `ppo_12_26` masked so it cannot emit a
duplicate value on a session with no price bar. Guard scan broadened to
`execution/`, `risk/`, `airflow/`, `scripts/`, `data/`. See the "non-pct_change
fabrication sweep" section of `docs/plans/01b1-pct-change-inventory.md`.

## P1 / High findings

### BUG-011: Approval gate trusts any DB row with matching run ID and SHA

**Severity:** High / authorization

**Evidence:** The Airflow approval sensor accepts a row in `blotter_approvals` matching run ID and expected SHA, then pushes selected IDs and quantity overrides. There is no authenticated operator boundary, signer verification, role check, or DB least-privilege separation. The approval table accepts arbitrary JSON selected IDs/overrides.

**Impact:** Any actor with DB write access can approve, deselect, or alter orders.

**Suggested direction:** Move approval through an authenticated service/API, store immutable principal/session evidence, validate selected IDs/overrides against the artifact, and restrict DB write privileges.

### BUG-012: Circuit breaker is UI-local/in-memory and not enforced at Airflow submission

**Severity:** High / trading safety

**Evidence:** The dashboard circuit breaker is a Streamlit cached singleton. The paper risk/compliance script explicitly forces `circuit_breaker_open=False`, and the Airflow submission path does not query a durable circuit-breaker state before submitting.

**Impact:** Airflow can submit orders while the dashboard shows an open breaker, or after a breach that is not represented in the DAG process.

**Suggested direction:** Persist circuit-breaker state in Postgres/Redis with audit records and require submission tasks to fail closed if it is open or unavailable.

### BUG-013: Service exposure and weak/no auth defaults create high-impact compromise paths

**Severity:** High / security

**Evidence:** Compose publishes Postgres, Redis, MinIO, MLflow, Airflow, Prometheus, Loki, and Grafana ports. Redis lacks auth/TLS, MLflow binds to `0.0.0.0`, and broad DB access can affect approval/control-plane state.

**Impact:** Running the stack on a shared workstation, remote VM, Codespace, or exposed network can expose secrets, data, and order-approval controls.

**Suggested direction:** Bind local-only services to `127.0.0.1`, avoid publishing internal services, require auth, and split DB users by privilege.

### BUG-014: Dashboard approval identity is spoofable or can be `unknown`

**Severity:** High / auditability

**Evidence:** Dashboard session state derives `operator_email` from `OPERATOR_EMAIL`, defaulting to `unknown`, and writes it directly into approval records.

**Impact:** The audit trail can contain approvals by `unknown` or any environment-provided string, weakening non-repudiation.

**Suggested direction:** Require authenticated dashboard users and store immutable identity metadata and signed approval payloads.

### BUG-015: Blotter approval UI can present/approve the wrong pending run

**Severity:** P1 / approval workflow

**Evidence:** The dashboard recursively scans `**/blotter*.json` under the artifact directory and returns the newest unapproved run. It is not bound to the currently waiting Airflow DAG run or XCom-published path/hash.

**Impact:** A stale, copied, test, or attacker-placed blotter can be presented first; the operator may approve the wrong artifact while Airflow keeps waiting or fails on mismatch.

**Suggested direction:** Bind the UI to Airflow metadata for the current waiting run, enforce canonical paths, and validate the exact expected hash.

### BUG-016: Blotter approval page does not validate full blotter schema before approval

**Severity:** P1 / safety gate validation

**Evidence:** The CLI submit validator requires exact schema/safety fields, but the dashboard only checks `candidate_rows_sha256` if present and then renders `candidate_rows`.

**Impact:** Operators can approve malformed artifacts that the downstream submit path rejects or that do not represent valid stage-only blotters.

**Suggested direction:** Share schema validation code between CLI/submission and dashboard, and block display/approval on validation errors.

### BUG-017: Quantity-reduction workflow updates `quantity` but validation still checks `estimated_shares`

**Severity:** P1 / workflow mismatch

**Evidence:** The UI stores reduced quantities in `quantity_overrides`; Airflow applies them to `quantity`; the validator checks `estimated_shares` instead.

**Impact:** Operator-approved quantity reductions may still fail validation on the original field, and validation does not necessarily cover the quantity that will be submitted.

**Suggested direction:** Make a single canonical submitted-quantity field and validate exactly that field after all overrides are applied.

### BUG-018: Project Python version metadata conflicts with Airflow image

**Severity:** P1 / runtime consistency

**Evidence:** `pyproject.toml` requires Python `>=3.12`, while the Airflow Docker image uses Python 3.11.

**Impact:** Installing the project into the Airflow image may be rejected, and development/test and orchestration runtimes are split.

**Suggested direction:** Align supported Python versions or use a Python 3.12 Airflow/runtime image if possible.

### BUG-019: MLflow artifact bucket config is ignored by MLflow server command

**Severity:** P1 / config drift

**Evidence:** `minio-init` creates `MINIO_BUCKET_MLFLOW`, but the MLflow server command hard-codes `s3://mlflow`.

**Impact:** Changing the bucket env var creates one bucket while MLflow writes to another.

**Suggested direction:** Use the same env var in the MLflow server command.

### BUG-020: Raw snapshot path logging ignores custom raw bucket names

**Severity:** P1 / auditability

**Evidence:** `save_raw_response()` writes to `MINIO_BUCKET_RAW` but returns/logs `rqis-raw/{key}` regardless of the configured bucket.

**Impact:** Audit/reprocessing metadata points to the wrong object location when the bucket is customized.

**Suggested direction:** Return the actual bucket/key used.

### BUG-021: Alembic migrations assume extensions exist outside migration control

**Severity:** P1 / database portability

**Evidence:** Compose init scripts create TimescaleDB/pgcrypto extensions, while migrations use `create_hypertable()` and `gen_random_uuid()` without ensuring extensions exist.

**Impact:** `alembic upgrade head` can fail on fresh non-Compose DBs, restored DBs, or old volumes missing extensions.

**Suggested direction:** Put extension creation/checks in migrations or an explicit required bootstrap step that fails clearly.

### BUG-022: Database init scripts are one-shot but required for secondary DBs

**Severity:** P1 / deployment footgun

**Evidence:** Compose relies on `/docker-entrypoint-initdb.d` to create `airflow` and `mlflow` databases; Postgres only runs those scripts on first volume initialization.

**Impact:** Existing volumes may lack required databases after scripts are added/changed, breaking Airflow/MLflow startup.

**Suggested direction:** Add an idempotent bootstrap command or documented migration/volume-reset procedure.

### BUG-023: PEG inverse rewards double-negative EPS/growth cases

**Severity:** P1 / factor definition

**Evidence:** The PEG inverse docstring says negative earnings/growth should rank poorly, but implementation multiplies earnings yield by growth. Negative EPS times negative growth becomes positive.

**Impact:** Loss-making companies with shrinking earnings can receive favorable scores.

**Suggested direction:** Require positive EPS and positive growth, or explicitly penalize negative components before scoring.

### BUG-024: Fundamental PIT safety is delegated to input dates without enforcement

**Severity:** P1 / lookahead risk

**Evidence:** Fundamental utilities state that `date` must be publication date, then forward-fill values to daily dates. There is no guardrail ensuring dates are actually public availability dates rather than fiscal period ends.

**Impact:** If period-end dates are supplied, value/quality/growth factors see fundamentals before public release.

**Suggested direction:** Require/validate an `available_at` field or enforce conservative reporting lags when true publication timestamps are absent.

### BUG-025: Missing-data weight renormalization creates coverage-driven alpha

**Severity:** P1 / ranking comparability

**Evidence:** Composite blending and top-level scoring redistribute missing signal/factor weights to available factors per row.

**Impact:** A stock with one available signal can receive a full-strength composite score, while another with complete coverage receives a diversified score. Non-random missingness can leak coverage/universe effects into alpha.

**Suggested direction:** Track coverage counts/effective weights, require minimum coverage, and optionally penalize or neutralize missingness.

### BUG-026: Indicator z-scoring drops all-tie or one-name dates

**Severity:** P1 / sample integrity

**Evidence:** Shared indicator z-scoring divides by cross-sectional standard deviation without handling zero/NaN std, while later composite blending has special handling.

**Impact:** Tied or one-name valid cross-sections become NaN and are silently dropped, altering the sample.

**Suggested direction:** Use robust z-score behavior consistently across indicator and composite layers.

## P2 / Medium findings

### BUG-027: Approval UI can crash on malformed/null quantities

**Severity:** P2 / UX robustness

**Evidence:** The approval page casts artifact/editor quantities with `int(...)` in multiple places without guarding against null, NaN, or non-numeric values.

**Impact:** Bad artifacts or invalid edits can crash the page instead of showing a clear validation error.

**Suggested direction:** Reuse downstream numeric validation before rendering and after editing.

### BUG-028: Dashboard artifact scanning lacks strong containment checks

**Severity:** P2 / confused-deputy risk

**Evidence:** `pending_blotter()` recursively opens recent `blotter*.json` files under an environment-controlled directory.

**Impact:** A process with write access to the artifact tree can influence what the dashboard presents for approval.

**Suggested direction:** Restrict to canonical current-run paths, reject symlinks, verify owner/mode, and validate schema before display.

### BUG-029: Live-trading clearance flag names differ across dashboard and broker

**Severity:** P2 / operational safety

**Evidence:** Dashboard live enablement uses `C8_CLEARED=true`; `IBKRBroker` requires `PAPER_RUN_CLEARED=true`.

**Impact:** UI and broker can disagree about whether live trading is cleared, creating confusing or unsafe runbooks.

**Suggested direction:** Use one durable, audited clearance state rather than multiple env vars.

### BUG-030: Airflow retries are risky for broker submission/control-plane actions

**Severity:** P2 / idempotency

**Evidence:** The DAG uses global retries and the submission task can retry once. Existing retry idempotency depends on a local reconciliation artifact that currently fails open when corrupt.

**Impact:** Partial failures can re-enter submission logic and duplicate orders.

**Suggested direction:** Make broker submission non-retrying unless broker-backed idempotency is implemented and fail-closed reconciliation is in place.

### BUG-031: Fundamental growth definitions use daily-row shifts after forward-fill

**Severity:** P2 / factor definition

**Evidence:** Multi-year and YoY growth indicators align fundamentals to daily dates and then shift by trading-day counts.

**Impact:** Report-event concepts can compare stale forward-filled values rather than exact quarterly/yearly report lags.

**Suggested direction:** Compute growth on report-event/fiscal-period series, then align the finished PIT metric to daily prices.

### BUG-032: `pivot_table()` silently averages duplicate ticker/date records

**Severity:** P2 / data quality

**Evidence:** Shared price and fundamentals helpers use `pivot_table`, which aggregates duplicates by mean by default.

**Impact:** Duplicate vendor rows, restatements, or split-adjustment duplicates can become synthetic averaged values without failing.

**Suggested direction:** Validate uniqueness before pivoting or use `pivot()` to fail loudly.

### BUG-033: Prometheus scrape target is not portable or backed by a Compose app service

**Severity:** P2 / monitoring correctness

**Evidence:** Prometheus scrapes `host.docker.internal:8000`, but Compose defines no app service on port 8000 and no Linux `extra_hosts` mapping.

**Impact:** Monitoring can show a dead scrape target in local Linux environments.

**Suggested direction:** Add the service/mapping or remove/update the scrape target.

## P3 / Low findings

### BUG-034: Performance table formats decimal returns as percentages incorrectly

**Severity:** P3 / UI correctness

**Evidence:** The Performance page computes returns as decimal fractions, then displays them with a percent suffix without multiplying by 100.

**Impact:** Returns/drawdowns can be understated by 100x in the UI.

**Suggested direction:** Multiply decimal fractions by 100 before percent-suffix formatting, or use Streamlit percentage formatting that expects fractions.

### BUG-035: No FastAPI/API route layer exists despite API/service-boundary expectations

**Severity:** P3 / architecture clarity

**Evidence:** Repository-wide searches found no FastAPI/APIRouter/route layer; Streamlit pages call DB query helpers directly.

**Impact:** Any expected backend validation/service boundary is absent; safety validation must be duplicated in UI and DAG code unless an API layer is added.

**Suggested direction:** Either document Streamlit-direct architecture and centralize validation libraries, or introduce an authenticated backend API boundary.

## Second-pass additions

The following findings were added after a second adversarial pass focused on gaps in data/storage/backtesting, portfolio/risk/execution, and packaging/CI/docs.

## P0 / Critical second-pass findings

### BUG-036: Package builds are blocked by an invalid PEP 517 backend

**Severity:** P0 / packaging blocker

**Evidence:** `pyproject.toml` declares `build-backend = "setuptools.backends.legacy:build"`, but the current environment cannot import that backend; `pip wheel . --no-deps --no-build-isolation` fails with `Cannot import 'setuptools.backends.legacy'`. The same metadata references `README.md`, which is not present at the repository root.

**Impact:** CI, Docker builds, deployments, and downstream consumers that build a wheel can fail before the project is installable.

**Suggested direction:** Use a valid backend such as `setuptools.build_meta` or `setuptools.build_meta:__legacy__`, restore/update the referenced README, and add a wheel-build CI check.

## P1 / High second-pass findings

### BUG-037: Same-date corporate actions overwrite each other

**Severity:** P1 / data correctness

**Evidence:** Corporate-action adjustment factors are stored in a dict keyed only by `ex_date`; split and dividend actions on the same date assign to the same key. The schema permits multiple actions for the same ticker/date when `action_type` differs.

**Impact:** A same-day split plus dividend can drop one adjustment, materially corrupting adjusted historical prices, backtests, and signal returns.

**Suggested direction:** Accumulate all action multipliers per ex-date and multiply them together, or key by `(ex_date, action_type)` before aggregating.

**Status (2026-07-20, branch `dev/R2-03A3-samedate-actions`, implemented-pending-review):**
`compute_adjustment_factors` (`data/normalization/corporate_actions.py`) now
accumulates the PRODUCT of every action's per-action multiplier for a given
`(ticker, ex_date)`, regardless of `action_type`, via a new
`_combine_same_date_action_multipliers` helper — shared automatically by
`build_score_price_history_as_of` and `build_realized_total_return_as_of`
(both 01B-3 cutoff-aware builders call the same function).

Same-date split+dividend quoting convention was verified empirically, not
assumed: yfinance's `Ticker.dividends` is confirmed (via AAPL, whose
2012-08-09 pre-split $2.65/share dividend is returned as $0.094643 == 2.65 /
(7*4), divided by the 2014 and 2020 splits that occurred strictly after that
date) to retroactively normalize dividend values against a ticker's full
split history — i.e. dividend values are always expressed in current/
post-split share-count terms. A 21-ticker S&P 500 same-date
`dividends`/`splits` collision scan (DHR, IRM, TMUS, EXPE, etc.) found only
spinoff-modeling artifacts, not genuine simultaneous ordinary split+dividend
rows, so the boundary case itself could not be tested against a real row;
the module adopts `POST_SPLIT` as the declared default convention based on
the general retroactive-normalization evidence, documented in the module
docstring. An optional per-row `dividend_quoting_convention` column
(`"post_split"`/`"pre_split"`) allows explicit override/normalization; any
other value, or a `"pre_split"` dividend with no resolvable same-date net
split ratio, raises a new `AmbiguousSameDateActionError` (fail closed rather
than guess).

New tests in `data/tests/test_corporate_actions.py`
(`TestSameDateSplitDividendAccumulation`,
`TestSameDateAccumulationFlowsThroughAllThreeCallers`) cover: a hand-computed
post-split split+dividend fixture, a pre-split-quoted fixture that converges
to the same combined factor after normalization, split+spinoff, a
three-same-date-action fixture, both `AmbiguousSameDateActionError` paths,
and that the fix flows through all three callers. All prior
`test_corporate_actions.py` and `backtesting/tests/test_engine.py` tests pass
unchanged.

**Operator sign-off on the POST_SPLIT default (2026-07-20, adversarial
review P1-1 resolution).** The operator reviewed and signed off on KEEPING
`POST_SPLIT` as the default convention rather than failing closed on all
same-date split+dividend rows. Rationale (operator): the ~21 real same-date
`dividends`/`splits` collisions found in the S&P 500 scan are Yahoo
spinoff-modeling artifacts stored as split+dividend row pairs, not genuine
ordinary simultaneous split+dividend events; a hard fail-closed default
would therefore break scoring today for those tickers with no genuine
ordinary same-date split+dividend case existing to justify it. The strong
general yfinance retroactive-normalization evidence (the AAPL 2012-08-09
demonstration above, holding uniformly across the whole dividend series) is
accepted as satisfying design-plan §3.1's "empirically determined"
requirement, even though the exact same-date boundary is untested against a
real row because no such row exists in the available universe. The two
untested residuals this leaves are tracked as BUG-076. (The design-doc-side
record of this sign-off is landed separately on branch 03A-1, which owns the
design plan; this ledger entry is the code-branch-side record.)

**Reachability note (adversarial review P1-2, fixed on this branch).**
`AmbiguousSameDateActionError` is a `ValueError` subclass and was reachable
by the pre-existing broad `except Exception` in `_write_simulation`'s
corporate-action adjustment helper in
`airflow/dags/daily_signal_pipeline.py` (the diagnostic
`strategy_simulations` path), which would degrade to raw prices under the
generic `simulation_corporate_action_adjustment_unavailable` event —
indistinguishable from an infra outage (the BUG-039 shape). Fixed by
special-casing the exception with a distinguishable
`simulation_ambiguous_same_date_action` structlog event before the broad
handler; the diagnostic table is intentionally kept non-blocking (still
falls back to raw prices), only made observable. The SCORE path
(`build_score_price_history_as_of`) is a separate call site and correctly
does NOT swallow the exception (verified by reviewer). See BUG-076 P2 note.

### BUG-076: Same-date corporate-action convention residuals (boundary untested; Yahoo spinoffs normalized as ordinary split+dividend)

**Severity:** P2 / data correctness (residual of BUG-037)
**Fix priority:** F2
**Status:** Open (tracked residual; operator-accepted for now per BUG-037 sign-off)

**Context:** Filed as the tracked residual of the BUG-037 fix
(`dev/R2-03A3-samedate-actions`) after the operator signed off on keeping the
`POST_SPLIT` default rather than failing closed. Two distinct residuals:

1. **The same-date ordinary split+dividend boundary is unverified against a
   real row.** The `POST_SPLIT` convention rests on strong *general* yfinance
   retroactive-normalization evidence, but no genuine simultaneous ordinary
   stock split + ordinary periodic cash dividend row exists anywhere in the
   available S&P 500 universe to test the exact boundary directly. If such a
   row ever appears (new data source, index change, or a non-yfinance
   Phase 2+ vendor), the assumed post-split dividend basis must be
   re-verified for that source before trusting the combined factor.

2. **Yahoo spinoffs modeled as same-date split+dividend rows are normalized
   as ordinary split+dividend, not as spinoffs.** The ~21 real same-date
   collisions (DHR 2016-07-05, IRM 2014-09-26, TMUS 2013-05-01, EXPE
   2011-12-21, etc.) are Yahoo's spinoff-modeling artifacts — one large
   one-time "dividend" plus a compensating "split"-labeled ratio — which the
   BUG-037 accumulator now multiplies together as if they were an ordinary
   split and an ordinary cash dividend. This can misstate the adjustment for
   those specific dates. Proper spinoff handling remains unimplemented
   (module docstring already flags spinoffs as not-implemented and requires a
   paid data source). Until then, adjusted prices on those exact dates should
   be treated as approximate.

**Also tracked here (P2 sub-notes, pre-existing / future-work, no code change
on the 03A-3 branch):**

- **The `dividend_quoting_convention` override is not wired to any DB read
  path** (Codex review round-2 P2, 2026-07-20). The optional
  `dividend_quoting_convention` column that drives `pre_split` normalization
  and the `AmbiguousSameDateActionError` fail-closed branch is a
  **forward-looking hook only**: it is never `SELECT`ed by the live DB read
  paths (the Airflow score/simulation queries in
  `airflow/dags/daily_signal_pipeline.py` and
  `scripts/validate_signal_ic.py` all select only `ticker, ex_date,
  action_type, value, known_at, source_version`), and no migration or writer
  creates the column. Consequently, for every real DB-sourced row the
  convention is always absent -> the `POST_SPLIT` default (operator
  signed-off 2026-07-20), and the `pre_split` and
  `AmbiguousSameDateActionError` branches are exercised only by explicit
  in-memory callers and tests. This is intentional for this slice — no
  `pre_split` data source exists yet, so gold-plating a migration + query
  changes now would be speculative. Fully activating the override (a column
  migration, updated SELECTs across all read paths, a writer, and a real
  dated `pre_split` source to justify it) is tracked future work. A code
  comment at the convention-lookup site in
  `data/normalization/corporate_actions.py` records the same reachability
  gap so it is not mistaken for an active DB-wired path.

- **NaN dividend / zero-or-negative split ratio are silently ignored**
  (adversarial review P2-3, pre-existing, not a regression). In
  `_combine_same_date_action_multipliers` / `compute_adjustment_factors`, a
  same-date split with `value == 0` contributes no multiplier, and a computed
  dividend factor `<= 0` is skipped; a NaN dividend `value` would flow into
  the Decimal factor as NaN and be dropped by the `factor > 0` guard rather
  than raising. These are silently no-op'd rather than surfaced. Acceptable
  for now (matches long-standing behavior) but should become an explicit
  validation/warning in a future data-quality pass.

- **Cutoff-filtering (§2.3) × convention-normalization (§3.1) interaction
  edge case** (adversarial review P2-4, future 03B/03C work). If a same-date
  split is excluded by the `known_at` availability cutoff while its same-date
  dividend survives the cutoff, a `pre_split`-quoted dividend would have no
  surviving same-date split ratio to normalize against and would raise
  `AmbiguousSameDateActionError` (fail closed) — or, under the default
  `post_split` assumption, be applied without the split context. The
  cutoff-aware builders filter actions *before* calling
  `compute_adjustment_factors`, so the convention-normalization step sees
  only the surviving subset. This interaction is benign under the current
  `POST_SPLIT` default but must be revisited when 03B/03C build the
  raw-execution vs analytic split, since the surviving-subset composition can
  differ between the two legs.

### BUG-038: Snapshot versions are mutable because date-only object keys are overwritten

**Severity:** P1 / reproducibility

**Evidence:** Snapshot writes use keys based on `{data_type}/{snapshot_date}/data.parquet`; pinning scripts and manifests use the caller-provided snapshot date as the version. Re-running the same snapshot date overwrites the same object path.

**Impact:** Prior backtests/manifests that point to a snapshot date can later resolve to different bytes, breaking reproducibility.

**Suggested direction:** Make snapshot paths content-addressed or run-id-addressed, refuse overwrite by default, and store/verify content hashes in manifests.

**Status update (03A-1, branch `dev/R2-03A1-content-addressed-snapshots`, Implemented-pending-review):** `data/storage/parquet_snapshots.py::save_snapshot` now keys objects by a canonical LOGICAL content hash of the DataFrame (`data/storage/canonical_hash.py::canonical_content_sha256` -- sorted rows/columns, normalized dtypes, SHA-256 of the canonical row stream), not by the caller-supplied `snapshot_date`, and not by raw parquet bytes (parquet byte output is not deterministic across writer versions). Re-saving identical logical content is a verified no-op (existing object is re-downloaded and re-hashed, not merely "key exists"); different content always gets a different key, so nothing already written is ever overwritten. `backtesting/dataset_manifest.py::DatasetManifest` gained per-data-type `content_sha256` for all four bundle types (generalizing the prior `alpha_scores_sha256`-only coverage), a `manifest_content_sha256` (hash of the manifest's own canonical JSON excluding provenance-only fields) that is now the intended MLflow `data_version`, and a `legacy_mutable` flag. `load_snapshot`/new `load_snapshot_by_manifest` re-verify content on load and raise the new `SnapshotIntegrityError` on mismatch. `backtesting/loader.py::load_from_snapshot` (the backtest read path) was updated to consume the new API: it resolves the bundle manifest from `data_version` (now a `manifest_content_sha256`) via `load_manifest`, then loads each data type through `load_snapshot_by_manifest`, preserving all prior loader behavior (strategy filtering, fail-closed empty-scores guard, optional corporate-actions fallback) with no raw-vs-analytic split (03B, out of scope). A new `tests/test_pin_snapshot.py::test_backtest_loader_reads_a_pinned_bundle_end_to_end` pins a bundle and loads it back through `load_from_snapshot` end-to-end against a fake MinIO so this read path cannot regress invisibly. Two other date-keyed readers (`scripts/backfill_momentum_scores.py`, `scripts/audit_pit_safety.py`) were repointed to a new `ParquetSnapshots.load_snapshot_legacy(data_type, snapshot_date)` that reads the retained pre-03A-1 date-keyed objects (design §5.1) unchanged. `scripts/pin_snapshot.py` uses the new content-addressed path and now also collects the informational per-data-type `bytes_sha256` (via `save_snapshot(..., bytes_sha256_out=...)`) into the manifest. A new `scripts/backfill_legacy_manifests.py` flags pre-03A-1 date-keyed manifests as `legacy_mutable: true` in place, read-only with respect to the underlying snapshot data objects. Deferred to later 03A phases: the full fail-closed object-store error taxonomy beyond `SnapshotIntegrityError` (03A-2), and wiring `eligibility_batch_id`/`membership_import_batch_id`/`research_methodology_id` plus the `BacktestLogger.log_run()` `data_version` cutover (03A-5). Tests: `data/tests/test_canonical_hash.py`, `data/tests/test_parquet_snapshots.py`, `backtesting/tests/test_dataset_manifest.py`, `tests/test_backfill_legacy_manifests.py`, `tests/test_pin_snapshot.py` (idempotent-re-pin and changed-row cases added).

**Hostile-review follow-up (03A-1, three findings fixed):** (1) `backtesting/dataset_manifest.py::load_manifest` previously returned whatever bytes sat at the caller-supplied key with NO integrity check, so a tampered/bit-rotted manifest at a content-addressed key -- the C7 `data_version` root that every leaf dataframe is trusted against -- was trusted blindly. `load_manifest` now recomputes the loaded manifest's own canonical hash and, for content-addressed (64-hex) versions, requires it to equal the key, raising `SnapshotIntegrityError` on mismatch (and also rejecting a manifest that claims `legacy_mutable` while sitting at a content-addressed key, closing the bypass). Legacy date-string versions still load unverified. `SnapshotIntegrityError` was promoted to `data/storage/errors.py` so both loaders share it without a circular import. (2) `canonical_content_sha256`'s row encoding was not injective -- a cell containing the field/row separator byte could shift column boundaries and collide two logically-distinct frames; fields (and rows) are now length-prefixed (`{len}:{value}`) so the encoding is injective regardless of cell contents. (3) **Known limitation (accepted):** under the corruption-detection threat model, `None`, `NaN`, and empty-string values in a string column all normalize to `""`, so a nulled/dropped string value is indistinguishable from a legitimately-empty one in the canonical hash. This is by design (dtype-drift robustness) and accepted for 03A-1; a future phase wanting to distinguish null from empty would need a distinct null sentinel in `_normalize_value`. New tests: `TestEncodingInjectivity` (embedded-separator collision), `TestLoadManifestIntegrity` (tamper → raise, untampered → load, legacy date-string → load, legacy_mutable-at-content-key → raise).

### BUG-039: Object-store errors become `FileNotFoundError`, causing corporate-action fail-open behavior

**Severity:** P1 / backtest correctness

**Evidence:** Snapshot loading converts any `S3Error` to `FileNotFoundError`; the backtest loader treats missing `corporate_actions` as optional and substitutes an empty DataFrame.

**Impact:** Auth, timeout, bucket-policy, or transient object-store failures can silently produce unadjusted backtest prices.

**Suggested direction:** Convert only true no-such-key/object-not-found errors to `FileNotFoundError`; re-raise other object-store failures and consider requiring corporate-action snapshots for production backtests.

**Status update (03A-2, branch `dev/R2-03A2-failclosed-objectstore`, Implemented-pending-review):** `data/storage/errors.py` gained the full typed hierarchy: `SnapshotNotFoundError` (subclasses `FileNotFoundError` for one deprecation cycle -- MinIO `NoSuchKey`/`NoSuchBucket` only), `SnapshotStoreUnavailableError` (connection/timeout/DNS/TLS and any other unexpected object-store error), `SnapshotAccessDeniedError` (403-class auth codes), and `SnapshotPartialReadError` (byte-count/Content-Length mismatch or parquet footer parse failure), alongside the existing `SnapshotIntegrityError`. `data/storage/parquet_snapshots.py` is now the single translation boundary: `translate_object_store_error()` maps `minio.error.S3Error` codes (and lower-level `urllib3`/socket/OS connection failures) to the typed hierarchy, and the new `get_object_bytes()` helper is the shared low-level read primitive (used by `load_snapshot`, `load_snapshot_legacy`, `_object_exists`, `_read_object_bytes`, and by `backtesting/dataset_manifest.py::load_manifest` via a lazy import, so that module no longer imports/catches `S3Error` itself). `backtesting/loader.py::load_from_snapshot` gained an explicit `allow_missing_corporate_actions: bool = False` keyword-only parameter: default is now fail-closed (any error loading corporate_actions, including a genuine not-found, propagates and aborts construction); passing `allow_missing_corporate_actions=True` narrows the accepted fallback to `SnapshotNotFoundError` only -- store-unavailable/access-denied/integrity/partial-read errors always abort regardless of the flag. The same opt-in pattern was extended to `scripts/backfill_momentum_scores.py` (which already had an `--allow-raw-prices-on-missing-actions` opt-in from BUG-009 round 9; its `except FileNotFoundError` was narrowed to `except SnapshotNotFoundError`) and `scripts/audit_pit_safety.py` (a read-only diagnostic tool with an already-documented best-effort corporate_actions fallback; narrowed the same way so an infra/auth failure can no longer masquerade as "no corporate actions" even there). `scripts/validate_signal_ic.py` was audited and found to load `corporate_actions` directly from the DB via SQL, not from a MinIO snapshot -- out of scope for this taxonomy. A repo-wide guard test, `tests/test_s3error_containment.py` (mirrors `tests/test_pct_change_guard.py`'s pattern), fails if any production module outside `data/storage/parquet_snapshots.py` references `minio.error.S3Error` in code (comments/docstrings are exempted). New/updated tests: `data/tests/test_object_store_error_taxonomy.py` (20 tests: translation-boundary code mapping, `get_object_bytes` Content-Length check, `ParquetSnapshots.load_snapshot` end-to-end for store-unavailable/access-denied/not-found/partial-read), `backtesting/tests/test_engine.py` (`test_loader_aborts_on_missing_corporate_actions_by_default`, `test_loader_non_not_found_corp_actions_error_aborts_even_when_opted_in`, plus existing loader tests updated to pass `allow_missing_corporate_actions=True` and to raise `SnapshotNotFoundError` from their fakes instead of bare `FileNotFoundError`). Folds in BUG-077 (see that entry).

**Write-path hardening (03A-2 adversarial-review follow-up):** the adversarial review found `backtesting/dataset_manifest.py::save_manifest`'s idempotent-write existence probe still did a DIRECT `minio_client.get_object(...)` wrapped in a bare `except Exception: existing_bytes = None` -- bypassing the very translation boundary this slice establishes. A transient `SnapshotStoreUnavailableError`/`SnapshotAccessDeniedError` there would be swallowed as "object doesn't exist yet" and fall through to `put_object`, a write-path fail-OPEN. Fixed by routing that probe through `get_object_bytes()` and narrowing the except to `SnapshotNotFoundError` (only a genuine not-found means "not written yet"; every other error propagates and aborts the save), mirroring the `load_manifest` fix. Tests: `backtesting/tests/test_dataset_manifest.py::test_save_aborts_on_store_unavailable_never_writes`, `::test_save_aborts_on_access_denied_never_writes` (both assert `put_object` is never called). The dataset-manifest test fakes' not-found sentinel was migrated from a custom exception to a genuine `S3Error(NoSuchKey)` so the translation boundary classifies it correctly.

### BUG-040: Wash-sale guard checks the wrong order direction

**Severity:** P1 / compliance correctness

**Evidence:** The trade journal finds recent loss-realizing SELL fills, but compliance stores them under a misleading `recent_loss_buys` key and `_check_wash_sale()` immediately allows every non-SELL order, blocking only later SELLs.

**Impact:** Replacement BUYs after a loss sale can pass, while unrelated later SELLs can be blocked. The control misses the likely wash-sale exposure and creates false positives.

**Suggested direction:** Rename the context to loss-sale history and evaluate replacement BUYs inside the wash-sale window, with tests for both false-negative and false-positive cases.

### BUG-041: Sector concentration is computed but never breach-checked

**Severity:** P1 / risk monitoring

**Evidence:** `RiskSnapshot` exposes `max_sector_concentration`, and the monitor computes sector weights, but breach detection checks only drawdown, VaR, beta, and single-name concentration.

**Impact:** A portfolio can be highly concentrated in one sector without tripping any sector risk breach or circuit-breaker path.

**Suggested direction:** Add configurable sector concentration thresholds and include sector breaches in the circuit-breaker decision.

### BUG-042: IBKR order-ID timeout can leave a live broker order untracked locally

**Severity:** P1 / trading safety

**Evidence:** `IBKRBroker.submit_order()` calls `placeOrder()` before waiting for a nonzero order ID. If the ID is not assigned within the timeout, it raises while warning that the order may still be live. Callers can then mark the order rejected/failed or omit the broker ID from artifacts.

**Impact:** The broker can have a working order while local OMS/artifacts show failure, encouraging duplicate retry/manual action and leaving exposure unmanaged.

**Suggested direction:** Treat post-placement ID timeouts as an indeterminate state requiring broker reconciliation; capture any available local trade/client metadata and fail closed before retrying.

### BUG-043: Non-isolated test collection fails through MLflow/pkg_resources dependency drift

**Severity:** P1 / CI reliability

**Evidence:** `backtesting.experiment_tracking.mlflow_logger` imports `mlflow` at module import time. The requirements pin `mlflow==2.10.2` but do not constrain or include a compatible `pkg_resources` provider; `python -m pytest --collect-only -q` fails with `ModuleNotFoundError: No module named 'pkg_resources'` in the current environment.

**Impact:** CI/test collection can fail before tests run, depending on setuptools/pkg_resources availability.

**Suggested direction:** Pin/add a compatible setuptools/pkg_resources provider, upgrade MLflow, or lazily import MLflow inside logger methods to avoid unrelated collection failures.

### BUG-044: Package discovery excludes operational modules used by DAGs/tests/runbooks

**Severity:** P1 / packaging completeness

**Evidence:** Setuptools package discovery includes first-party domain packages but excludes `airflow*`, `scripts*`, and `config*`. DAGs import `scripts.*` and `config.universe_loader`, and tests import `scripts` directly.

**Impact:** A wheel install can omit operational modules that the source-tree runbooks and DAGs require, making installed deployments differ from source-checkout behavior.

**Suggested direction:** Decide whether the project is installable or source-tree-only. If installable, package operational modules or move them under a first-party namespace with console entry points.

### BUG-045: Local Airflow stubs shadow real Apache Airflow imports

**Severity:** P1 / test validity

**Evidence:** The repo contains a top-level `airflow` package described as a minimal local testing stub, including simplified `DAG` and `PythonOperator` implementations under the same import path as Apache Airflow.

**Impact:** Commands run from the repo root can test against stubs rather than real Airflow, masking real DAG parse/runtime incompatibilities.

**Suggested direction:** Move stubs under a test-only namespace or inject them via fixtures, and add a real-Airflow DAG import smoke test in an environment where the stubs are absent from `PYTHONPATH`.

## P2 / Medium second-pass findings

### BUG-046: Market-data backfill can mark partially loaded tickers complete

**Severity:** P2 / ingestion completeness

**Evidence:** The resume helper treats a ticker as done if it has at least one row in the first 31 calendar days of the requested window. The yfinance backfill then skips those tickers entirely.

**Impact:** An interrupted run that wrote a few early rows can permanently skip the remaining requested history, leaving sparse coverage that downstream snapshots/signals inherit.

**Suggested direction:** Validate coverage across the full requested range using latest date, expected row-count thresholds, and/or an exchange calendar.

### BUG-047: Data-quality flag deduplication has no conflict key

**Severity:** P2 / data-quality table correctness

**Evidence:** The writer uses `ON CONFLICT DO NOTHING` for quality flags, but the migration creates only a surrogate primary key and non-unique indexes; there is no unique constraint over the logical duplicate fields.

**Impact:** Repeated data-quality checks can insert duplicate flags indefinitely, bloating dashboards and overstating unresolved issues.

**Suggested direction:** Add an appropriate unique constraint/index, such as `(ticker, date, flag_type, message)` or `(ticker, date, flag_type)`, and report actual inserted rows.

### BUG-048: `trade_fills` dedup guard allows duplicate cumulative fills with different timestamps

**Severity:** P2 / trade journal correctness

**Evidence:** The migration comment says the unique constraint rejects re-recording the same order/cumulative quantity, but the actual unique constraint includes `fill_timestamp`.

**Impact:** The same cumulative fill can be inserted again with a slightly different timestamp, corrupting FIFO P&L, wash-sale history, and position reconstruction.

**Suggested direction:** Enforce broker execution IDs or `(order_id, cumulative_filled_quantity)` idempotency in a table/constraint that can represent the true invariant.

### BUG-049: Optimizer fallbacks can return portfolios that violate configured caps

**Severity:** P2 / portfolio construction

**Evidence:** MVO infeasible paths return equal weights without re-checking max-position or sector caps. Risk parity accepts constraints but does not enforce sector caps in the solver and only applies a post-hoc single-name cap.

**Impact:** A target portfolio can be labeled optimized while violating configured position/sector constraints.

**Suggested direction:** Re-validate every fallback output against all constraints and fail rather than returning an invalid portfolio when no feasible solution exists.

### BUG-050: NaN-heavy return series can suppress VaR/CVaR breaches

**Severity:** P2 / risk monitoring

**Evidence:** VaR/CVaR helpers check raw series length before dropping NaNs, and breach checks compare values directly. NaN values do not satisfy threshold comparisons.

**Impact:** Sparse or broken return streams can produce NaN risk metrics and fail open instead of triggering a data-quality/circuit-breaker breach.

**Suggested direction:** Require a minimum number of finite observations after `dropna()`, reject non-finite VaR/CVaR, and fail closed on insufficient risk data.

### BUG-051: Step 7 CLI can submit old but checksum-valid blotters

**Severity:** P2 / trading freshness

**Evidence:** Step 6 records generation/target/snapshot dates, but Step 7 validates schema and checksums without enforcing artifact age, target date, snapshot freshness, or re-running current account/risk checks.

**Impact:** A stale blotter can be submitted against stale prices, cash, positions, and target weights as long as checksums match.

**Suggested direction:** Enforce maximum artifact age and trading-date freshness at Step 7, and require current broker/account/risk checks immediately before submission.

### BUG-052: Airflow fire-drill runbook contradicts DAG timezone semantics

**Severity:** P2 / operational docs

**Evidence:** The data DAG is documented/configured for `20:00 America/New_York`, but the fire-drill runbook says the cron fires at `20:00 UTC`.  A secondary instance of the same confusion existed as the inline `# 21:30 UTC weekdays` comment on the `schedule_interval` line of `daily_signal_pipeline.py`.

**Impact:** Operators can expect or diagnose runs at the wrong wall-clock time, especially around DST.

**Fix (Session 56 + Session 57):** Runbook scheduling notes rewritten to say ET; UTC equivalents provided for EDT and EST separately.  Inline DAG comment corrected to `# 21:30 ET weekdays (01:30 UTC in EDT / 02:30 UTC in EST)`.

### BUG-053: `make check` mutates the working tree

**Severity:** P2 / CI hygiene

**Evidence:** The `check` target depends on `fmt`, and `fmt` runs `ruff format .`, which can rewrite files before lint/typecheck/tests run.

**Impact:** A validation command can silently change files and pass locally, leaving dirty-tree formatting changes unnoticed.

**Suggested direction:** Add a non-mutating `fmt-check` target using `ruff format --check .` and make `check` depend on that instead of `fmt`.

## P3 / Low second-pass findings

### BUG-054: Fundamentals backfill skip logic can leave partially ingested tickers stale forever

**Severity:** P3 / ingestion completeness

**Evidence:** The fundamentals backfill considers a ticker already ingested if it has any row in `financial_statements`, and skips it unless `--force` is used.

**Impact:** Interrupted or partial fundamentals ingestion can leave concept/period coverage incomplete while the script reports the ticker as already ingested.

**Suggested direction:** Track completeness by ticker, source version, concept set, and latest filing date; skip only when the expected coverage contract is satisfied.

### BUG-064: `_write_simulation` skips shadow strategies because XCom alpha_df only covers the current run's strategy

**Severity:** P2 / multi-strategy simulation correctness

**Evidence:** `alpha_df` in `_write_simulation` comes from `ti.xcom_pull(key="alpha_scores_json", task_ids="combine_scores")`, which only contains scores for the single `params['strategy_id']` of the current DAG run. The loop `alpha_df[alpha_df["strategy_id"] == strategy_id]` returns an empty frame for every other registered strategy and `continue`s, leaving the `strategy_simulations` table unpopulated for shadow strategies.

**Impact:** The multi-strategy comparison panel in the Performance dashboard (which reads `strategy_simulations`) never receives rows for any strategy except the one that ran today, defeating the purpose of the table.

**Fix (Session 57, PR #31 Codex comment #1):** The loop now checks whether the current `strategy_id` appears in the XCom data. If yes, it uses the in-memory frame (fast path). If no, it queries `alpha_scores` from the DB for that `strategy_id` and `score_date`. All registered strategies receive a simulation row on every pipeline run.

### BUG-065: `simulated_return` denominator uses n_long instead of len(tickers), understating returns when universe < n_long

**Severity:** P2 / simulated NAV correctness

**Evidence:** `target_weights` assigns `1/len(tickers)` to each selected position (weights sum to 100% regardless of universe size). When `len(tickers) < n_long`, dividing `sum(returns)` by `n_long=20` rather than `len(tickers)` understates the portfolio return. For example, a 10-name universe where all names return 1% records only 0.5%, corrupting the compounded NAV chain.

**Impact:** Simulated NAV for small-universe strategies is systematically biased downward; strategy comparison panels understate their performance vs. larger strategies.

**Fix (Session 57, PR #31 Codex comment #2):** Changed denominator from `n_long` to `len(tickers)`. When the universe has ≥ n_long names, `len(tickers) == n_long` and the result is identical. For smaller universes, the correct portfolio return is now computed. Tickers with missing prior-day prices continue to contribute 0% (cash-equivalent treatment).

### BUG-066: Cross-sectional scoring has no minimum-eligible-count enforcement

**Severity:** P2 / research validity

**Evidence:** The 01B design plan (`docs/plans/01b-research-validity-design.md` §3.1)
requires that cross-sectional scoring "report the resulting eligible count and fail
when the configured minimum cross-section is not met." Neither
`signals/scoring/scorer.py` nor the indicator-level
`cross_sectional_zscore`/`to_long` pipeline enforces any minimum: a date whose
cross-section has shrunk to a handful of tickers (or even one) still produces
z-scores and downstream alpha scores with no warning or failure.

**Impact:** Scores computed from a silently shrunken cross-section are statistically
meaningless but indistinguishable from healthy ones downstream. The BUG-010 fix
(01B-1) makes this more visible: raising rolling `min_periods` to full windows and
gating gapped windows correctly suppresses more per-ticker values, which *increases*
the frequency of shrunken cross-sections — correct per-ticker behavior, but the
missing aggregate gate means the shrinkage stays silent.

**Suggested direction:** Belongs to the 01B research-validity follow-up work (with
BUG-008/BUG-009): add a configurable minimum eligible count to the scorer, log the
per-date eligible count, and fail closed (or mark the date ineligible) when the
cross-section is below the configured minimum rather than imputing or silently
scoring a tiny universe.

**Origin:** Confirmed during the 01B-1 (BUG-010) adversarial review, 2026-07-16;
out of scope for that fix round.

### BUG-067: Universe loader returned an empty universe on fetch failure (fail-open)

**Severity:** P1 / pipeline integrity

**Status:** Fixed on `dev/R2-01B2-pit-universe` (01B-2 Phase 3).

**Evidence:** `config/universe_loader.py::_fetch_sp500_from_wikipedia` caught all
exceptions and returned `[]`, so a Wikipedia outage silently emptied the daily
ingestion universe and every downstream consumer.

**Impact:** A transient network failure could produce a zero-ticker ingestion run
with no task failure or alert; downstream scoring would quietly shrink.

**Fix:** The loader now raises `UniverseFetchError` (fail closed); Airflow retries
absorb transient outages. The affected test was deliberately updated. The module is
also now labeled operational-current-mode only (BUG-008 type-level separation).

### BUG-068: Wikipedia constituent history has bounded count drift

**Severity:** P2 / research data quality

**Status:** Open — documented limitation of the 01B-2 initial provider; remediate
at Gate 03 with a commercial constituent source (entity-level identifiers).

**Evidence:** 2026-07-17 verification import: per-date member counts are 417
(2000-01-03), 502 (2010-01-04), 522 (2020-01-02), 519 (2023-06-01) versus a true
~503-505. 245 of 890 intervals are left-censored (assumed start at 1976-07-01);
three ticker-collision symbols (AN, SUN, AGN) are excluded entirely. See
`docs/plans/01b2-constituent-source-contract.md`.

**Impact:** Mild universe over-inclusion in the recent era and under-coverage
pre-2000. The drift adds/retains names rather than excluding removed losers, so it
does not reintroduce the BUG-008 survivorship direction, but IC cross-sections can
include a small number of non-members in older periods.

**Suggested direction:** Replace/augment the Wikipedia import with a licensed
point-in-time constituent feed at Gate 03; the schema and runtime API are already
provider-agnostic (`source`/`source_version` columns, `ConstituentProvider`
protocol).

**Update (01B-2 fix round):** ticker-collision exclusions are now persisted as a
DB-queryable JSON audit record on `universe_import_batches.excluded_tickers` and
surfaced by `coverage_report()`, so the AN/SUN/AGN exclusions no longer live only
in logs and this document's companion contract doc.

### BUG-069: Signal DAG degrades to unfiltered provisional scores when the PIT universe is stale

**Severity:** P2 / operational monitoring

**Status:** Partially superseded by BUG-009 round 11 (see below) — the
"degrade and keep writing silently" half of the original operator
acceptance no longer holds for the WRITE path; the underlying "warn and
degrade" filtering behavior itself is unchanged.

**Original evidence:** `airflow/dags/daily_signal_pipeline.py::
_pit_membership_filter` deliberately logs a warning and proceeds without
membership filtering when no published universe import covers the score
date (the import advances coverage only when
`scripts/import_universe_membership.py` is re-run).

**Original operator decision (2026-07-18):** the warn-and-degrade behavior
is acceptable for now; daily operational scores remain available (paper
pipeline is not blocked), but they silently revert to provisional
current-membership semantics for research purposes with the only signal
being a structlog warning. Revisit flipping to hard-fail once the universe
import is on a scheduled cadence rather than run ad hoc — see
`Worklog.md` 2026-07-18.

**Round 11 supersession (same day, BUG-009 section 4):** adversarial
review found that the degrade path's silence was worse than originally
scoped — `_write_scores` tags every persisted row with the ACTIVE run for
`daily_signal_pipeline_operational`, whose registered methodology claims
`universe_import_policy = "pit_universe_effective_dated_v1"` (PIT-universe
safety). A degraded, unfiltered write under that methodology is not just
an availability trade-off, it is a provenance-honesty violation: any
reader trusting `research_run_id` to mean "PIT-certified" (which is the
documented, authoritative meaning per migration 012) would be misled with
NO indication beyond a log line. `_write_scores` now calls the shared
`data.research.sql_compat.assert_methodology_write_is_honest` gate and
FAILS CLOSED (raises, Airflow marks the task failed — real alerting,
unlike the prior "log warning only") whenever either independent PIT
lookup this DAG performs (`_load_prices` for momentum/lowvol/value,
`_compute_quality`'s own separate lookup) degraded. This deliberately
reverses the "daily operational scores remain available" half of the
2026-07-18 acceptance for the WRITE path specifically — a degraded day no
longer silently persists provisional scores under the PIT-claiming run,
it fails loudly instead. The underlying filtering/degrade LOGIC itself
(`_pit_membership_filter`, the per-task try/except degrade pattern) is
unchanged; only what happens to the resulting provisional rows at write
time changed.

**Impact:** A stale/unavailable PIT universe import now blocks that day's
`_write_scores` task entirely (visible Airflow task failure) rather than
silently writing provisional scores. This is a genuine operational
availability trade-off against the original 2026-07-18 decision, made
explicitly and flagged here rather than silently overridden — operators
should be aware the paper pipeline CAN now stall on a stale PIT import in
a way it did not before this round.

**Operator sign-off (2026-07-19):** presented explicitly as a choice
before merge — fail-closed on write (this behavior) vs. reverting to
warn-and-persist under an honestly-labeled provisional methodology. The
operator chose to accept fail-closed on write as-is.

**Suggested direction (residual, not yet implemented):** if continuous
availability during PIT degradation is still wanted, the correct design
is an auto-provisioned fallback methodology/run (idempotently registered
the same way `scripts/register_operational_research_run.py` registers the
operational one) whose `universe_import_policy` honestly declares no PIT
filtering, so `_write_scores` can keep persisting DURING a degrade
without lying about it — rather than either lying (pre-round-11) or
refusing to write at all (post-round-11). Deliberately not built this
round: a stateful "auto-register a second methodology from inside a DAG
task" mechanism is a meaningfully larger change than the honesty gate
itself and was judged out of scope to rush under review pressure (see
round 7/10's precedent for this same judgment call). Also still open: an
AlertManager hook or DAG-level SLA/telemetry when the filter degrades
(now at least partially superseded by the write failure itself acting as
an alert), and an Airflow maintenance task that re-runs the universe
import on a schedule.

### BUG-074: Registered operational methodology mislabels legacy corporate-action source version

**Severity:** P2 / research provenance precision

**Status:** Open. Codex review comment on PR #35 arrived 2026-07-19 05:14Z,
8 minutes before the operator merged the PR; never triaged or routed to a
fix before merge.

**Evidence:** Migration 011 backfills existing `corporate_actions` rows with
`source_version = 'legacy_unknown'` for a database with prior data, but
`scripts/register_operational_research_run.py` registers the operational
methodology's `action_source_version` as plain `"unknown"`. The daily scorer
reads the full action history (including migrated legacy rows) for its
adjusted price panel, so any score whose lookback touches a migrated
split/dividend row is produced with a mix of `legacy_unknown` and current
ingestion-sourced actions, but the run's registered methodology only claims
`"unknown"` — imprecise, not a safety violation (both labels correctly
convey "not a real vendor version string"), but not fully truthful either.

**Impact:** Low. This is a metadata-precision gap, not a data-integrity or
lookahead issue — no incorrect adjustment or lookahead results from it. It
affects only how precisely the run's provenance record describes its inputs.

**Suggested direction:** Either register the methodology with a value that
covers both `legacy_unknown` and the current ingestion source (e.g.
`"mixed(legacy_unknown,yfinance-current)"`), or normalize/backfill the
migrated legacy rows to the same source_version convention before activating
the operational run, whichever better matches the intended long-term
provenance model.

### BUG-070: Backtester uses a single full-history adjusted price series for both scoring and execution

**Severity:** P1 / backtest validity (discovered during 01B-3, BUG-009 follow-on)

**Status:** Open. Discovered while implementing 01B-3
(`docs/plans/01b-research-validity-design.md` §2.3-2.4) and deliberately left
unfixed here: the task scope for 01B-3 explicitly excludes touching execution
code, and §2.4's own required-changes list treats this as a distinct,
separately-scoped item ("Make the backtester use the raw execution series
plus explicit corporate-action accounting, then compare its total-return
valuation to the analytic series").

**Evidence:** `backtesting/loader.py::load_from_snapshot` calls the
full-history `compute_adjustment_factors`/`apply_adjustment_factors` routine
once over the entire price history (own docstring: "This loader applies
corporate-action adjustment factors before constructing DataHandler so the
backtest engine always operates on split- and dividend-adjusted closes") and
passes the single resulting adjusted series into `DataHandler`, which serves
it to both signal computation and simulated fills. 01B-3 added two
cutoff-aware alternatives
(`data/normalization/corporate_actions.build_score_price_history_as_of` /
`build_realized_total_return_as_of`) but the backtester does not use them.

**Impact:** A future corporate action within the loaded snapshot window can
still adjust a historical score/signal's input price in the backtester
specifically (the same class of lookahead BUG-009 fixed for
`signals/research/ic.py`), and the backtester's fills are computed from an
adjusted (not raw) price series rather than the raw tradable price the
design plan requires for order/cash notional (§2.2). This does not affect
IBKR paper/live order pricing (execution/ uses IBKR-quoted prices directly,
not this loader).

**Correction (adversarial review, same day):** an earlier draft of this
entry additionally claimed "this does not affect `signals/research/ic.py`
(fixed by 01B-3)". That was **false** as applied to the live IC-validation
entrypoint: `scripts/validate_signal_ic.py` and
`scripts/backfill_momentum_scores.py` loaded raw `close` directly from
`daily_prices`/the price snapshot with ZERO calls to the new cutoff-aware
builders — the timing bug (BUG-009's namesake same-close lookahead) was
fixed in `signals/research/ic.py` itself, but the adjustment gap this
BUG-070 entry describes was equally present in the live callers, not just
the backtester. **Fixed same-day**: both scripts now call
`build_score_price_history_as_of` (price-ratio-based factors: momentum,
lowvol) and `build_realized_total_return_as_of` (the forward/realized-return
leg for every factor) before prices reach `compute_ic_series`/
`compute_momentum_scores`. See BUG-071 for the residual limitation of that
fix (a single run-boundary cutoff, not a literal per-score-date cutoff).
`backtesting/loader.py` remains the one open instance of this class of gap
after that fix, which is why this entry (BUG-070) stays open.

**Suggested direction:** Split `backtesting/loader.py` into two series per
the design plan: a raw execution series (for fills/cash/share accounting,
with explicit split-share-adjustment and dividend-cash-accounting in the
portfolio path) and a cutoff-aware analytic series
(`build_score_price_history_as_of` for anything feeding signal computation,
`build_realized_total_return_as_of` for total-return valuation/reporting).
Reject a requested backtest run when the required corporate-action data
cannot be constructed for either series, rather than silently falling back
to `adj_factor=1.0`.

**Resolution (Implemented-pending-review, branch
`dev/R2-03B-backtester-series-split`, 2026-07-20):** `backtesting/loader.py`
now passes the RAW (unadjusted) `daily_prices` series to `DataHandler`
unmodified -- the only series `BacktestEngine` uses for fills/NAV/share
accounting (`DataHandler.get_close`). Corporate actions on a held position
are applied explicitly in `_PortfolioState.apply_corporate_actions`
(`backtesting/engine/event_loop.py`): a split multiplies the held share
count by the net split ratio; a dividend credits cash by
`shares_held * value` (post-split, matching the module's declared
POST_SPLIT convention); spinoffs are logged and ignored, matching the
legacy routine. `DataHandler.get_corporate_actions_on(sim_date)` surfaces
same-date action rows; `BacktestEngine.run` applies them before any
NAV/weight computation for that date.

Backtester NAV is already total-return-correct via this raw-price +
explicit dividend-cash / split-share accounting: a dividend on a held
position credits cash and a split adjusts the share count, so the NAV path
captures total return without ever adjusting a traded price. **That is the
part of BUG-070 delivered and wired end-to-end.**

A separate cutoff-aware ANALYTIC price series is additionally BUILT (but,
in this slice, NOT YET CONSUMED by any reporting/attribution code path)
via `build_realized_total_return_as_of` (a single run-boundary cutoff --
`session_close_cutoff(min(backtest.end_date, latest loaded price date))` --
the same accepted convention 01B-3 already uses for
`scripts/backfill_momentum_scores.py`/`scripts/validate_signal_ic.py`; see
BUG-071 for the documented residual limitation, which this loader
inherits) and exposed via `DataHandler.get_analytic_close`. It is scaffolding
for FUTURE total-return reporting/attribution consumers -- it is never used
for fills, and no tearsheet/attribution/report reads it today. The tested
accessor is retained deliberately as the stable contract that future
reporting will consume; wiring it into a consumer is tracked as BUG-079. No
live signal computation from prices happens inside the backtester itself
(alpha_scores arrive pre-computed from the snapshot, already fixed upstream
by 01B-3), so `build_score_price_history_as_of` was not needed inside
`loader.py`.

A within-window corporate-action ex_date that has no aligned trading
session in the loaded price calendar is rejected fail-closed at
`DataHandler` construction (P1 fix): the event loop only applies actions on
sim_dates present in the price calendar, so a split/dividend on a
price-gap day would otherwise be silently dropped and permanently corrupt
the share count (e.g. a dropped 2:1 split undercounts shares 2x thereafter).
Actions outside the loaded window are correctly ignored (never applied) and
do not trip the gate.

Fails closed per 03A-2's existing taxonomy: `allow_missing_corporate_actions`
still governs whether a genuinely absent `corporate_actions` snapshot may be
treated as "no actions" (empty frame, both raw execution and analytic
series unaffected); any other snapshot error, or a `corporate_actions`
frame structurally missing `known_at`/`ex_date` when non-empty, propagates
uncaught from `build_realized_total_return_as_of` and aborts backtest
construction -- never a silent `adj_factor=1.0` degrade.

Tests added in `backtesting/tests/test_engine.py` /
`backtesting/tests/test_loader_series_split.py` (see branch): a future
corporate action cannot change a historical raw close via `get_close`; a
split is applied as a share-count change (not a price adjustment) and a
dividend as a cash credit, both verified against hand-computed NAV; and a
fail-closed test proving a missing corporate_actions snapshot aborts by
default and only proceeds with explicit `allow_missing_corporate_actions=True`.

### BUG-071: Score-series cutoff-aware adjustment uses one run-boundary cutoff, not a literal per-score-date cutoff

**Severity:** P2 / research validity (narrow residual gap, discovered during
01B-3's P0 fix round; re-verified and re-scoped in round 4 after a
related, more severe finding on the REALIZED-RETURN leg)

**Status:** Open for the SCORE leg only, re-verified and re-scoped —
CLOSED (fixed) for the realized-return leg, which this bug originally
also covered. Do not conflate the two: they turned out to need different
verdicts.

**Round-4 history (read this first):** adversarial review round 4 found
that the single-boundary-cutoff shortcut this bug originally described for
BOTH the score series and the realized-return series was actually UNSAFE
for realized returns — an action not yet knowable at an EARLIER exit
date's own cutoff, but knowable by the later shared boundary, was
incorrectly included in that earlier exit's return: future information
leaking into a persisted, "PIT-safe" result. That was a real defect, not a
narrow edge case, and is now FIXED: `scripts/validate_signal_ic.py` uses
`signals.research.ic.compute_realized_forward_returns_as_of`, which builds
a genuinely per-exit-date-cutoff-correct series (one
`build_realized_total_return_as_of` call per distinct exit date, not one
shared boundary) — see the fixing commit for the full derivation and a
reproduction test (`TestComputeRealizedForwardReturnsAsOf` /
`TestRealizedForwardReturnsWiring`).

Per the same round's instruction, the SCORE series' original cancellation
argument was re-examined rather than assumed still valid: `scripts/
validate_signal_ic.py::_build_score_adjusted_prices` (renamed from
`_build_adjusted_price_series`, which no longer exists — it built both
series with the same flawed approach) still uses one boundary cutoff, and
this remains provably safe for a DIFFERENT structural reason than the
realized-return case: a score's ratio is always computed between two dates
INSIDE the same lookback window, both on the same side of any action whose
`ex_date` is after the window's end (the score_date) — there is no second,
later "exit" endpoint the way a realized return has, so the failure mode
round 4 found cannot arise here. See `_build_score_adjusted_prices`'s
docstring for the full re-verified derivation.

**Residual gap for the score leg:** the one case a literal per-score-date
cutoff would treat differently is an action whose `ex_date` falls exactly
ON a given score_date — not yet knowable at that exact session's own close
under the conservative rule, but includable once the global run boundary
has passed it. This is a single-session edge case per affected
ticker/action, not the "zero adjustment happens at all" gap the original
P0 fix closed, and not the "future information leaks across an entire
holdout" class of bug round 4 found (and fixed) in the realized-return
path.

**Suggested direction (score leg only):** if full per-score-date
correctness is later required for scores too, either (a) loop
score-date-by-score-date building a distinct adjusted series per date
(correctness at the cost of losing the vectorized pass's performance —
`signals.composites.*` compute an entire panel in one call, so this would
require restructuring those composites, not just this script), or (b)
special-case actions whose `ex_date` falls inside the union of all
requested score dates and mask just those specific (ticker, score_date)
pairs after the vectorized computation.

### BUG-072: Dashboard/diagnostic readers of alpha_scores/factor_scores are not filtered to the active research run

**Severity:** P2 / display correctness (discovered during 01B-3's
adversarial-review round 3; every reader except `scripts/
indicator_diagnostic.py` closed by round 7)

**Status:** Fixed. Round 8 closed the last open item
(`scripts/indicator_diagnostic.py`) — see below. Round 3 of adversarial
review on PR #35 found
that `scripts/paper_inputs_check.py` (and, through it,
`scripts/paper_target_check.py`/`paper_order_candidates_check.py`/
`paper_risk_compliance_check.py`/`paper_stage_blotter_check.py`),
`airflow/dags/daily_signal_pipeline.py`'s two internal readers, and
`scripts/pin_snapshot.py` all read `alpha_scores`/`factor_scores` without
filtering to the explicitly active `research_run_id` — the production-
safety-critical instances were fixed in round 3 (see BUG-009's status
notes). Round 4 (Codex, independently) flagged the same gap in
`reporting/dashboards/queries.py`'s `latest_alpha_scores`,
`bottom_alpha_scores`, and `factor_scores_for_ticker` and it was fixed the
same round: each now joins against an `_ACTIVE_RUN_SUBQUERY` filtering to
the active `daily_signal_pipeline_operational` run, degrading to an empty
result (not a crash) when none is active. New tests
(`tests/reporting/dashboards/test_sprint3_queries.py::
TestActiveResearchRunFiltering`) prove a stale/inactive run's colliding row
is excluded and that no-active-run degrades gracefully. Round 6 closed
`pipeline_health()`'s signals-recency check the same way — it previously
ran `MAX(computed_at)` over ALL of `alpha_scores`, so a fresher row left
behind by an inactive/superseded run could make the dashboard report the
pipeline healthy while the active run was actually stale or absent; new
tests (`tests/reporting/dashboards/test_pipeline_health.py`) reproduce
exactly that scenario and prove the active run's own staleness now wins.
Round 7 closed the remaining two: `reporting/dashboards/queries.py`'s
`alpha_score_at_fill_date`/`factor_scores_at_fill_date` (Sprint 4
audit-trail drill-down queries) and `reporting/dashboards/simulation.py`'s
`alpha_overlap_matrix` now filter via the same `_ACTIVE_RUN_SUBQUERY`
(`simulation.py` imports it directly from `queries.py` rather than
duplicating the SQL string, so the two modules cannot drift out of sync).
New tests in `tests/reporting/dashboards/test_sprint4.py`
(`TestAlphaScoreAtFillDate`, `TestFactorScoresAtFillDate`, and
`TestAlphaOverlapMatrix::test_inactive_run_row_excluded`) cover all three.
A round-7 repo-wide grep for every remaining `FROM alpha_scores`/
`FROM factor_scores` confirmed no other reader is unfiltered without a
documented, deliberate reason (see below, at the time — round 8 later
tightened the one remaining item).

**Round 8 (Codex, closing the entry):** Codex made a fair case that leaving
`scripts/indicator_diagnostic.py` open as a deliberate exception (round 7's
"research/diagnostic tool" rationale) was not actually justified once
migration 012 widened `factor_scores`' PK/unique constraints to include
`research_run_id` — this tool's own duplicate-row detector
(`backtesting/validation/indicator_diagnostic.py`'s
`indicator_diagnostic_duplicate_rows` check) only *warns* on duplicate
`(ticker, score_date, factor_name)` rows and then `pivot_table` silently
*averages* them, so a mixed-run blend could reach the reliability/validity
report with no indication it happened — a real correctness risk for a tool
whose entire purpose is measuring factor reliability/validity, not just a
staleness/display issue like the dashboard cases. Fixed:
`scripts/indicator_diagnostic.py::_load_factor_scores` now defaults to the
same `_ACTIVE_RUN_SUBQUERY` pattern used everywhere else under this bug
(scoped to the active `daily_signal_pipeline_operational` run). A new
`--all-runs` flag is the documented, explicit opt-in for genuine cross-run
diagnostic comparisons (the design plan's "explicit opt-in for cross-run
reads" principle) — and unlike the old default behavior, `--all-runs`
itself now fails closed (raises `ValueError`) if the resulting blend
contains duplicate `(ticker, score_date, factor_name)` rows, rather than
letting them reach the pivot. New tests in
`tests/test_indicator_diagnostic_script.py` cover: default active-run
scoping excludes a colliding inactive-run row, `--all-runs` raises on a
real collision, `--all-runs` succeeds when runs don't collide (disjoint
date ranges), and no-active-run degrades to an empty (not crashing) result.
`scripts/pin_snapshot.py` remains the one intentionally different pattern
(mandatory disambiguation via `--research-run-id` when a collision is
detected, rather than an active-run default) — that asymmetry is
deliberate: `pin_snapshot.py` pins a specific bundle an operator names
explicitly, while every other reader (including this one, now) defaults to
"whatever is currently operational."

**Resolution:** No remaining open scope. This entry is fully closed.

### BUG-073: pytest `testpaths` silently excluded ~412 tests from every "full suite" run

**Severity:** P1 / test-coverage integrity (self-discovered while verifying
round 8's fixes, 01B-3)

**Status:** Fixed.

**Evidence:** `pyproject.toml`'s `[tool.pytest.ini_options]` `testpaths`
listed `"tests"` AND `"tests/strategy_registry"` (a subdirectory already
inside `"tests"`) as two separate entries. Whenever pytest resolves this
full multi-entry `testpaths` list with no explicit command-line path
arguments — i.e. exactly how every round's "run affected suites + full
suite once" step in this review series actually invoked it — the presence
of that redundant nested entry silently dropped two entire subtrees from
collection: `tests/reporting/dashboards/` (113 tests — including
`test_pipeline_health.py`, `test_sprint3_queries.py`, `test_sprint4.py`,
`test_blotter_approval.py` — the exact active-research-run-filtering
regression tests added in rounds 4, 6, and 7 of this very review) and
`tests/infra/` (Airflow image/compose smoke tests), totaling 411 missing
tests (2096 truly-collectible minus 1685 actually collected). Removing the
explicit CLI path arguments and instead adding/removing individual
`testpaths` entries via `-o testpaths=...` overrides bisected the cause to
that single redundant nested entry — removing it alone (without changing
anything else) restored collection from 1685 to the full 2096.

**Impact:** Every "full suite: N passed, 0 failed" status reported to the
PM across rounds 1-8 of this review series was accurate for the tests it
ran, but was NOT actually the full suite — it silently omitted the
dashboard active-run-filtering regression tests and the Airflow
image/compose smoke tests for the entire review. Because this was
discovered and fixed with the true full suite immediately re-run and
passing (2096 passed, 0 failed, including every previously-hidden test), no
actual regression was found to have been hiding behind this gap in this
case — but the gap itself was real and could have hidden one.

**Fix:** `pyproject.toml`'s `testpaths` no longer lists
`"tests/strategy_registry"` separately; `"tests"` alone already covers it
recursively. Verified the corrected config collects 2096 tests (up from
1685) with `python -m pytest --collect-only -q`, and that all 2096 pass.

**Suggested direction (residual):** consider adding a CI-time guard (e.g. a
lightweight test or lint step) that asserts `pytest --collect-only` finds
every `test_*.py` file under the configured testpaths roots, to catch a
similar silent-exclusion regression before it can recur — not implemented
here to avoid scope creep on an already-large PR, but worth a follow-up.

### BUG-075: Backtest path silently ignored unsupported strategy-config fields (portfolio.method, constraints, risk_model, live-only execution fields)

**Severity:** P0 / research-validity (Roadmap row 02B)

**Status:** Implemented — pending review (branch `dev/R2-02B-config-failclosed`).

**Evidence:** `config/strategy/v2_mvo_momentum.yaml` declares
`portfolio.method: mvo`, `portfolio.optimizer_mode: max_sharpe`, a
`constraints` section (`max_sector_weight`, `max_portfolio_beta`,
`min_order_notional`), a `risk_model` section (Ledoit-Wolf covariance
methodology), and live-only `execution` fields (`broker: ibkr`,
`paper_trading: true`, `algo: market`). Before this fix,
`backtesting/engine/event_loop.py::BacktestEngine.run` never branched on
`portfolio.method` at all -- `_select_equal_weight` ran unconditionally
regardless of what the config declared -- and never read `constraints`,
`risk_model`, `portfolio.optimizer_mode`, or the live-only `execution`
fields anywhere. A backtest run against `v2_mvo_momentum.yaml` and logged
to MLflow as "mvo with a 25% sector cap and a 1.5 beta ceiling" was
silently, indistinguishably an uncapped equal-weight backtest. This is the
same defect class BUG-009 fixed for the write path (a persisted artifact's
label not matching what was actually computed), on the strategy-config axis
instead. A related, narrower instance of the same bug: `v2_mvo_momentum.yaml`
nests `data_version` under `backtest:` rather than at the top level, but
`BacktestEngine.run` only ever reads top-level `config["data_version"]` --
so as originally written, a v2 backtest run would silently produce an empty
`BacktestResult.data_version`, only surfacing as a C7 failure at the much
later MLflow-logging step instead of immediately at config-validation time.

**Impact:** Any backtest, walk-forward validation, or parameter-sensitivity
sweep run against a config declaring an unimplemented portfolio method,
constraint, risk model, or live-only execution field silently produced
results mislabeled with the declared (unimplemented) methodology instead of
the actual (equal-weight, unconstrained) one -- exactly the kind of
research-validity misrepresentation this project's C7 methodology-honesty
discipline exists to prevent, just not previously enforced on this axis.

**Fix:** Added `backtesting/config_contract.py` as the single shared
enforcement point (mirrors `data.research.sql_compat.assert_methodology_write_is_honest`,
BUG-009 section 4). `validate_backtest_config()` classifies every config
field as CONSUMED (read and behavior-changing), INFORMATIONAL (declared,
harmless -- pure metadata or a different subsystem's already-tracked knob,
e.g. `universe.*`/`indicators.*` describing upstream signal methodology, or
`portfolio.target_volatility` which belongs to the live/paper sizing path),
or unlisted -- which fails closed. `portfolio.method` is value-restricted to
`"equal_weight"`, the only method the engine actually runs; `constraints`
and `risk_model` sections, `portfolio.optimizer_mode`/`drift_threshold`,
`execution.broker`/`paper_trading`/`algo`, and a nested `backtest.data_version`
are all explicitly rejected. `UnsupportedStrategyConfigError` deliberately
does not subclass `ValueError`/`RuntimeError` so existing broad `except`
clauses (e.g. `ParameterSweeper.sweep`'s per-variant data-error handling,
which intentionally records data-availability failures as a single NaN
variant and continues) cannot swallow a config rejection into a
warn-and-continue path -- it always propagates and halts. Wired into every
backtest-path call site that accepts a raw config dict:
`BacktestEngine.run`, `backtesting.loader.load_from_snapshot`,
`WalkForwardValidator.run`, `ParameterSweeper.sweep`,
`BacktestLogger.log_run`, and `BacktestLogger.log_walk_forward_run`.
`bootstrap_stress`/`SurvivalFunnel` take an already-computed
`WalkForwardResult`, not a raw config, so they inherit validation
transitively. A conformance test suite
(`backtesting/tests/test_config_contract.py`) flattens every key in every
`config/strategy/*.yaml` file and asserts each resolves to an explicit
CONSUMED/INFORMATIONAL/rejected classification (never "unknown") via
`config_contract.field_status()`, so a future PR that adds a new,
unreviewed key to an existing strategy YAML fails CI instead of the key
passing through silently unclassified. `v1_base_momentum.yaml` passes
validation unchanged (no code change to that file was required);
`v2_mvo_momentum.yaml` is now rejected with all nine of its unsupported
fields/sections listed in a single error instead of silently downgrading.

**Adversarial-review round-2 fixes (APPROVE-WITH-FIXES, 2026-07-19):** two
P0 findings, both instances of "contract classification doesn't match
actual code reads", fixed on the same branch:

1. *P0-1 — `execution.*` was classified CONSUMED but nothing read it.*
   `BacktestEngine.run` takes an already-constructed `FillSimulator`; no
   committed code built it from `config["execution"]` (only a
   documentation example in `.claude/skills/backtest.md`), so declared
   cost params were unverifiable claims. Fixed by adding read-only
   introspection properties to `FillSimulator` and a new
   `assert_fill_simulator_matches_config()` in `config_contract.py`
   (raising `ExecutionConfigMismatchError`, a subclass of
   `UnsupportedStrategyConfigError`) which `BacktestEngine.run` calls to
   fail closed whenever declared `execution:` params differ from the
   simulator's actual params. One pre-existing engine test was itself
   carrying this exact mismatch (declared `fill_model: perfect`, ran a
   `transaction_cost` simulator) and was corrected.
2. *P0-2 — `name` was classified INFORMATIONAL but the loader consumes it*
   as the alpha_scores strategy_id fallback, and a name/stored-id mismatch
   filtered scores to EMPTY with only a warning — a silent cash-only
   backtest under the strategy's declared name. Fixed both ways:
   `name` reclassified CONSUMED (fallback kept so v1 works unchanged,
   explicit `strategy_id` documented as preferred), and
   `load_from_snapshot` now RAISES when zero score rows match the
   resolved strategy_id.

Post-fix sweep re-audited every classification against every keyed config
read in the five backtest-path modules: `version` was also reclassified
CONSUMED (read for the MLflow `strategy_version` tag);
`created`/`description`/`backtest.benchmark`/`universe.*`/`indicators.*`/
`reporting.*`/`portfolio.target_volatility` confirmed unread by any keyed
read (bulk verbatim recording via `_log_params_flat`/config.json/
config_hash interprets no specific key and does not count). P2 fixes:
`field_status()` now returns a distinct `"section"` status for bare known
section names, and the conformance test documents the flattening
depth-cap invariant.

**Scope note:** This fix does NOT implement MVO/risk-parity/beta-constraint
semantics inside the backtest engine -- that remains explicitly out of
scope here (and would collide with the separate Roadmap 03B loader work,
BUG-070). It only makes the backtest path honest about what it does and
does not implement, by rejecting configs that claim more than the engine
delivers rather than silently running a degraded version of what was
asked. Implementing MVO/risk-parity inside `BacktestEngine` remains a
distinct, larger future roadmap item; until then, `v2_mvo_momentum.yaml`
(or any future MVO/risk-parity strategy config) cannot be backtested at
all -- it can only be run through the live/paper execution path, which
already implements these methods.

### BUG-078: Strategy-config eligibility filters have no PIT source and were unenforced

**Severity:** P1 / research validity (same defect class as BUG-008/BUG-009,
on the eligibility axis instead of membership/timing)

**Status:** Phase A merged (PR #41). Phase B implemented — pending
operator/PR review (03A-4b, branch `dev/R2-03A4b-eligibility-population`).

**Context:** `docs/plans/03a-immutable-research-data-design.md` §1, itself
carved out of `docs/plans/01b-research-validity-design.md` §1.3: "If a
historical version of a configured filter is not available, exclude it from
the baseline contract... do not substitute today's market cap, ADV, halted,
or bankruptcy state." Before this fix, `data/universe/runtime.py` answered
membership only (`load_universe_as_of`, BUG-008); every strategy-config
eligibility filter (`config/strategy/v1_base_momentum.yaml`'s
`universe.min_market_cap_usd`/`universe.min_adv_usd`,
`v2_mvo_momentum.yaml`'s `universe.filters.{min_market_cap_usd,
min_adv_usd, min_price_usd, allowed_security_types}`) had no runtime
enforcement path at all -- any future caller that read these keys naively
would have had to substitute a current value, which is precisely the
lookahead-shaped defect 01B §1.3 forbids.

**Phase A fix (this branch, schema + read API + fail-closed config
contract only):**

* **Schema** (`infra/db/migrations/versions/013_universe_eligibility_attributes.py`,
  revision `013`, down_revision `012`): `universe_eligibility_batches`
  (append-only computation-run provenance, mirrors
  `universe_import_batches`) and `universe_eligibility_attributes`
  (append-only, effective-dated fact table -- one row per `(universe_id,
  ticker, attribute_name, effective_start)`, half-open interval, a CHECK
  constraint enforcing exactly one of `attribute_value_numeric`/
  `attribute_value_text` is populated, a CHECK constraint enforcing
  `source_data_asof <= effective_start` (future-leak guard), and a
  `computation_batch_id`-scoped `EXCLUDE USING gist` no-overlap constraint
  mirroring migration 009's pattern). Mirrored ORM models added to
  `data/universe/models.py` (`UniverseEligibilityBatch`,
  `UniverseEligibilityAttribute`), same cross-dialect divergence already
  documented for `UniverseMembership` (the gist EXCLUDE constraint is
  Postgres-only and not declared at the ORM level).
* **Runtime read API** (`data/universe/runtime.py`): `FilterSpec`,
  `EligibilityResult`, `EligibilityExclusionReason`
  (`missing_attribute`/`stale_attribute`/`below_threshold`/`wrong_type`),
  `PITEligibilityLookup`/`load_eligibility_as_of` (evaluate declared filters
  as of a date; fails closed with `NoEligibilityDataError` when zero rows
  exist for a `universe_id` at all, distinct from a per-ticker
  `missing_attribute` exclusion), and `load_historical_universe_as_of`
  combining membership (BUG-008) and eligibility into one call site
  (`CombinedEligibleUniverse`) so no future caller can apply one check
  without the other.
* **Fail-closed config contract** (`data/universe/eligibility_config.py`,
  new module): classifies every eligibility-shaped strategy-config filter
  key as `PIT_SUPPORTED` (`min_adv_usd`/`min_price_usd`/
  `allowed_security_types` -> `adv_usd_20d`/`price_usd`/`security_type`) or
  `FAIL_CLOSED_UNSUPPORTED` (`min_market_cap_usd`, `max_market_cap_usd`,
  `halted_flag`, `bankruptcy_flag` -- all named explicitly per the binding
  operator decision that yfinance has no filing-dated shares-outstanding
  source for market cap, and 01B §1.3's explicit halted/bankruptcy
  carve-out). `parse_universe_eligibility_filters` raises
  `UnsupportedEligibilityFilterError` (collecting every violation, not just
  the first) for any key not `PIT_SUPPORTED` -- including a brand-new,
  never-reviewed key, which classifies `UNCLASSIFIED` and is rejected
  identically to a named-unsupported one so nothing can silently pass by
  omission. A conformance test
  (`data/tests/universe/test_eligibility_config.py`,
  `test_every_universe_filter_key_is_explicitly_classified`) enumerates
  every `universe.*` filter key actually present in every
  `config/strategy/*.yaml` file (bottom-up, not invented from the design
  doc) and asserts none resolve to `UNCLASSIFIED`; both shipped configs are
  confirmed to fail closed on `min_market_cap_usd` by name.

**Phase B fix (this branch, 03A-4b -- data population + scoring-path
wiring):**

* **Daily batch job** (`data/universe/eligibility_batch.py`,
  `compute_price_eligibility_rows`/`write_price_eligibility_batch`,
  CLI: `scripts/backfill_eligibility_attributes.py`): populates
  `adv_usd_20d` (20-session trailing dollar-volume mean) and `price_usd`
  (raw close) from `daily_prices`, one row per (ticker, attribute, trading
  session), chained into half-open intervals with the latest date left
  open -- PIT-by-construction per design doc §1.4, never a
  current-value-projected-backward substitution. ADV rows require a full
  trailing window (fail-closed: no partial-window average is emitted).
  `market_cap_usd` remains permanently out of scope (module docstring +
  `EXCLUDED_ATTRIBUTES`), matching Phase A's `eligibility_config.py`
  rejection and migration 013's comment -- unchanged, per the binding
  operator decision (no dated yfinance shares-outstanding source).
* **`security_type` hand-curated backfill**
  (`SecurityTypeCurationEntry`/`build_security_type_rows`/
  `write_security_type_batch` in the same module, CLI:
  `scripts/import_security_type_curation.py`,
  seed source `data/vendor/security_type_curation/sp500_security_types.yaml`):
  `universe_import_batches`-style provenance per §5.1/§6 item 3. A ticker
  with explicit curated entries uses ONLY those entries (overlapping/
  invalid ranges rejected at build time); every other tracked member gets a
  default classification (`CS`) for its full known membership span, derived
  from the latest published `universe_membership` batch. The seed curation
  file intentionally ships with zero entries: fabricating unverified
  historical security-type-change dates for real public companies would
  itself be a data-integrity violation in a system that will eventually
  trade real capital. Every member is `CS`-classified by default until a
  researcher adds a verified, sourced entry.
* **Scoring-path wiring** (`scripts/backfill_momentum_scores.py`): new
  optional `--strategy-config` flag parses `universe.eligibility` filters
  via `data.universe.eligibility_config.parse_universe_eligibility_filters`
  (fail-closed, unchanged from Phase A) and, when filters are declared,
  constructs one `PITEligibilityLookup` alongside the existing
  `PITUniverseLookup` and combines them per score date into a
  `CombinedEligibleUniverse` (same object
  `data.universe.runtime.load_historical_universe_as_of` returns) so
  membership and eligibility are evaluated together for every scored
  ticker/date -- "no caller can apply one check without the other" is now
  true at the actual score-generation call site, not just at the unused
  API layer. Omitting `--strategy-config` preserves pre-03A-4b behavior
  (membership-only filtering) for backward compatibility.
  `--strategy-config` is rejected together with `--provisional-no-universe`
  (eligibility filters are evaluated against the same PIT membership that
  flag skips). `NoEligibilityDataError` propagates uncaught when filters
  are declared but the batch job has never populated the universe.
* **Coverage report** (`eligibility_coverage_report` in the same module,
  CLI: `scripts/eligibility_coverage_report.py`): mirrors
  `data/universe/import_pipeline.py::coverage_report`'s 01B-2 precedent --
  per-date/per-attribute row counts against real PIT membership, plus
  `security_type` curated-vs-default ticker counts and an explicit
  `excluded_attributes` listing (`market_cap_usd`) so the permanently
  out-of-scope attribute is a named exclusion, never a silent gap.

**Judgment calls / scope notes for reviewers:**

* The two shipped strategy configs (`config/strategy/v1_base_momentum.yaml`,
  `v2_mvo_momentum.yaml`) both declare `min_market_cap_usd` and were
  deliberately NOT modified (C6: neither has been used in a live session,
  but both have been used in the Phase 4 paper dry-run/probe, so a
  precautionary new-version-file discipline was applied rather than editing
  in place). Running `--strategy-config` against either YAML today still
  fails closed with `UnsupportedEligibilityFilterError` by design -- this is
  Phase A's existing, already-tested behavior reachable one layer further
  down the stack, not a regression. Migrating either config to the
  `universe.eligibility` block (dropping `min_market_cap_usd`) is a
  separate, config-owning decision left to the operator/PM, not made here.
* `--strategy-config` was added as an opt-in flag (default `None`,
  preserving legacy membership-only behavior) rather than a hard
  requirement, so existing callers/tests/DAG invocations of
  `scripts/backfill_momentum_scores.py` are unaffected until an operator
  explicitly opts a strategy into eligibility enforcement.
* `scripts/validate_signal_ic.py` (a sibling 01B-3 historical caller) was
  NOT wired to the combined check in this slice -- only the score-generation
  backfill script named in the 03A-4b task brief. Extending IC validation to
  the same combined check is a reasonable, low-risk follow-up if the
  operator wants eligibility enforced there too.

**New defect discovered while testing (recorded here, not fixed in this
slice -- outside 03A-4b's scope, lives in the shared momentum-scoring
pipeline rather than the universe/eligibility code this slice owns):** see
BUG-082.

**Tests:** Phase A (unchanged): `data/tests/universe/test_eligibility_runtime.py`
(11 tests), `test_eligibility_config.py` (22 tests), `test_eligibility_migration.py`
(4 tests). Phase B (new, this branch): `data/tests/universe/test_eligibility_batch.py`
(28 tests: `compute_price_eligibility_rows` grain/PIT-safety/chaining,
`write_price_eligibility_batch` persistence/append-only-ness,
`build_security_type_rows`/`write_security_type_batch` curated-vs-default
semantics and overlap rejection, `eligibility_coverage_report` gap detection
and out-of-scope-attribute reporting) and 5 new tests appended to
`data/tests/universe/test_backfill_universe_filter.py` (18 -> 23)
(`TestStrategyConfigEligibilityWiring`: deterministic partial-exclusion
proof via volume-differentiated ADV, permissive-threshold pass-through,
fail-closed on an unsupported filter reached through the scoring path,
`--provisional-no-universe` incompatibility guard, `NoEligibilityDataError`
propagation). Full `data/tests/universe/` suite: 237 passed (up from 194 at
03A-4a Phase A; 232 with just the Phase B core-module tests, 237 with the
scoring-path wiring tests too).

**03A-5 follow-up (2026-07-21, branch `dev/R2-03A5-manifest-linkage`,
downstream of BUG-078 but not itself part of it):** the manifest/methodology
linkage roadmap row (`docs/plans/03a-immutable-research-data-design.md`
§2.2/§2.4, §5.2's "03A-5" row) that ties a pinned `DatasetManifest` to the
exact `UniverseImportBatch`/`UniverseEligibilityBatch` this Phase B's data
feeds is now wired: `backtesting/dataset_manifest.py::build_manifest` accepts
and fail-closed-validates `eligibility_batch_id`/`membership_import_batch_id`/
`research_methodology_id`; `scripts/pin_snapshot.py` looks these up from the
real DB; `BacktestLogger.log_run`/`log_walk_forward_run` gained an opt-in
`require_manifest_data_version` check rejecting non-hash-shaped
`data_version` values. This was scoped in the roadmap as its own phased row
(03A-5), not as a standalone bug, so no new BUG-XXX was opened for it.

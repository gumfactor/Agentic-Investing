# 04-4W — Complete the identity change: thread, record, and seal-safely evaluate the window

**Status:** Scoped (PM, 2026-08-07) in response to PR #49 Codex round-3, which
surfaced two P1s that are NOT independent nits but two faces of one unfinished
change. Operator direction: "if there is a larger fix here, scope it out — don't
whack-a-mole." This doc is that scope. Implemented into PR #49 (operator chose to
land it here rather than split to a separate slice).

Prereq reading: `docs/plans/04-identity-evaluation-context-design.md` (the
operator-approved decision to exclude `backtest.start_date/end_date` from
`config_hash`).

## 1. The root cause (one thing, not four)

The identity decision moved the evaluation window (`backtest.start_date/end_date`)
out of `config_hash` and into "evaluation context." That was implemented on the
**identity/dispatch side** (fingerprint excludes the window; guards bind runs to
the canonical hash). But a window that is no longer encoded by the hash must be
**threaded, recorded, and validated as a first-class per-measurement input** —
and the **record/evaluate side was not updated to match.** Every PR #49 review
round is the same defect resurfacing one layer deeper:

| Round | Symptom | Layer | Status |
|---|---|---|---|
| R1 | promotion run accepted any `config_hash`, not the registered canonical one | dispatch (bind identity) | fixed `68cb0a8` |
| R2 | definition-reuse silently kept the stored window over the caller's | registration (reuse guard) | fixed `cb9b4f3` |
| **R3-A** | holdout confirmation runs the multi-fold walk-forward (needs ~6 yr) over a 6-month holdout, and **burns the one-shot seal on the setup error** | **evaluate (seal-safety + evaluator)** | **this slice** |
| **R3-B** | each trial records only the coarse `window='train_oos'` label, **not the actual eval dates** — measurements are under-identified now the hash omits them | **record (persist the window)** | **this slice** |

R1/R2 completed the identity/dispatch side. **04-4W completes the
record/evaluate side.** Both R3 findings fall out of the same root, so they are
scoped and built together, not patched independently.

## 2. Work items

### W1 — Persist the effective evaluation window on every trial (R3-B)

The window is already computed at the recorder (`_effective_range(config)` →
`effective_start`/`effective_end`, `trial_recorder.py:362,455`) and used for the
holdout guard, then **thrown away** at the `StrategyTrial(...)` insert
(`trial_recorder.py:687`, and the sweep insert at `:745`). Persist it.

- **Migration 015** (`infra/db/migrations/versions/015_strategy_trial_eval_window.py`):
  add `eval_start_date DATE` and `eval_end_date DATE` to `strategy_trials`.
  New rows: NOT NULL (enforced by the writer). Legacy backfill: add nullable,
  then the writer never emits NULL for new rows (mirror the `hypothesis_id`
  nullable-for-legacy-only precedent already documented on the model). Do **not**
  raw-`ALTER` (C2) — Alembic op only.
- **Model** (`strategy_registry/selection_models.py`): add the two mapped columns
  next to `window`. Keep them adjacent to `window`/`run_type` for readability.
- **Writer** (`trial_recorder.py`): pass `eval_start_date=effective_start`,
  `eval_end_date=effective_end` into **both** `StrategyTrial(...)` inserts. The
  values already exist in scope — this is wiring, not recomputation.
- Optional but recommended: a partial index or at least confirm the
  `window↔run_type` CHECK still holds; no CHECK change needed.

Result: a train_oos measurement and a holdout measurement under the same
`config_hash` are now distinguishable and reconstructable from the row alone,
which is the property the hash used to provide before the window left it.

### W2 — Seal-safe, holdout-appropriate evaluation (R3-A)

Two independent bugs, both fixed here:

1. **Wrong evaluator for holdout.** Holdout confirmation is semantically a
   **single fixed-config backtest over the sealed holdout window** — there is no
   walk-forward *within* the one-shot holdout (you get one look, not a rolling
   series of folds). Today `promotion_pipeline.py:589` dispatches holdout_mode
   through `self._wf_validator` (the multi-fold `WalkForwardValidator`, which
   requires ~`3*252` train days + three 12-month folds ≈ 6 yr). Route holdout_mode
   to a **single-window evaluator** (one `BacktestEngine.run` over the holdout
   window, wrapped by the recorder as the `holdout_confirmation` trial) instead of
   the fold-based validator. Train/OOS path is unchanged.
2. **Seal consumed before the evaluation is viable.** `run_walk_forward` commits
   the unique `holdout_confirmation` row (which trips
   `uix_strategy_trials_one_holdout_confirmation`, consuming the one-shot seal)
   **before** the evaluator runs (`trial_recorder.py:687` insert → validator runs
   after). A setup/insufficient-data failure therefore burns the seal permanently;
   even a corrected retry is rejected. Add a **data-sufficiency + config-viability
   preflight that runs BEFORE the seal-committing insert**, and fail closed there
   (raise, no row) if the holdout window can't actually be evaluated. Only an
   attempt that is genuinely viable — i.e. actually gets to *look* at the holdout
   returns — may consume the seal. Preserve the existing intent that a run which
   reads the holdout and then crashes still counts (that is a real look); the fix
   is strictly that *pre-look setup errors must not seal.*

   Design care: the seal semantics (one look, ever) must not be weakened. The
   preflight only checks *availability/shape* of the holdout window's data (dates
   present, enough sessions for a single backtest), which is not "a look" at
   returns for evaluation. Document this boundary explicitly on the preflight,
   the same way the seal's before-dispatch insert is documented today.

### W3 — Tests (foreground, must be green before report)

- Holdout confirmation over a realistic ~6-month holdout window **succeeds** end
  to end (single-window evaluator, seal consumed exactly once).
- Insufficient-data / non-viable holdout window → preflight **raises and the seal
  is NOT consumed**; a subsequent corrected attempt is still allowed (assert the
  partial-unique index is untouched after the failure).
- `eval_start_date`/`eval_end_date` are **persisted and queryable** on both a
  train_oos trial and a holdout trial, and differ when the windows differ.
- Migration 015 up/down runs clean on SQLite (test path) and the model matches
  the migration (existing metadata-vs-migration parity test, if present).
- Full suite stays green (current baseline: 217 in
  promotion_pipeline+trial_recorder+registry; run the broader suite too).

## 3. Numbering / coordination

- This slice takes **migration 015**. The planned 04-5 `validated`-status
  migration shifts to **017** (see §5 below) now that migration **016** is
  taken by the round-4 completion sweep.
- No new roadmap PR: 04-4W lands **inside PR #49** per operator decision.
- `holdout_mode` stays enabled (it was re-enabled with the identity fix); this
  slice makes it actually correct rather than re-gating it.

## 4. Explicitly out of scope

- No change to `config_hash` semantics (settled in the identity design doc).
- No change to the train/OOS promotion evaluator or funnel/sensitivity/stress.
- No `validated`-status work (that is 04-5).

## 5. Round-4 completion sweep (PR #49 Codex round-4, bounded follow-up)

Codex round-4 found two remaining surfaces of the SAME two defect classes
this doc scoped in §1 -- not new classes, the record/evaluate side had not
been closed out completely. Both are now closed:

- **R4-B (Class 1: persist the window) extended to `strategy_runs`.** §2 W1
  persisted `eval_start_date`/`eval_end_date` on `strategy_trials`
  (migration 015). `strategy_runs` (`strategy_registry/models.py`) has
  `data_version` but had no evaluation window, so two `backtest`/
  `walk_forward` runs over different windows recorded under the same
  `strategy_id`/`config_hash`/`data_version` were indistinguishable.
  **Migration 016** (`infra/db/migrations/versions/016_strategy_run_eval_window.py`)
  adds the same two nullable DATE columns to `strategy_runs`, mirroring
  015 exactly. `StrategyRegistry.record_run` now requires
  `eval_start_date`/`eval_end_date` (both non-null, start <= end) exactly
  for `run_type in {'backtest', 'walk_forward'}` -- the same
  `_REQUIRE_DATA_VERSION` set used for the existing C7 gate -- and leaves
  them optional for `unit`/`signal_ic`/`paper`/`live`. The CLI
  (`strategy_registry/cli.py record-run`) gained
  `--eval-start-date`/`--eval-end-date`. `PromotionDecision` and MLflow
  logging were left untouched (out of scope, per the original task scope:
  `PromotionDecision` is a rollup over trials that already carry the
  window; MLflow logging is a sink covered by the existing C7 data_version
  gate).

- **R4-A (Class 2: evaluator-owned preflight) closes the drift, not just the
  one missing check.** §2 W2's holdout preflight
  (`trial_recorder._preflight_holdout_viability`) hand-coded a checklist
  that had to mirror `BacktestEngine.run`'s own pre-data-read setup, and it
  had already drifted: it never called
  `assert_fill_simulator_matches_config`, so a holdout config whose
  `execution`/`fill_model` mismatched the dispatched evaluator's
  `FillSimulator` passed preflight, the seal-committing row inserted, and
  only then did `BacktestEngine.run` raise `ExecutionConfigMismatchError`
  before reading any prices -- permanently burning the one-shot seal on a
  pre-look setup error. The fix extracts that setup phase (config-contract
  validation + fill-simulator/config agreement + non-empty resolved
  trading dates) into **one shared method**,
  `BacktestEngine.validate_runnable(config, data_handler, fill_simulator)`
  (`backtesting/engine/event_loop.py`), which `BacktestEngine.run` itself
  now calls instead of duplicating the checks.
  `SingleWindowEvaluator.validate_runnable(config, data_handler)`
  (`backtesting/validation/holdout_evaluator.py`) delegates to that same
  method using the evaluator's OWN `self._engine`/`self._fill_sim`, so
  whatever setup THIS evaluator's engine performs before its first look is
  exactly what gets verified. `trial_recorder.run_walk_forward`'s
  `pre_insert_check` now delegates to the dispatched validator's
  `validate_runnable` (duck-typed via `hasattr`) when available, wrapping
  whatever it raises into `HoldoutEvaluationPreflightError`, and falls back
  to the hand-coded `_preflight_holdout_viability` only for a validator
  that does not provide one (e.g. a test double) -- so the two checklists
  can no longer independently drift for any real dispatch path.

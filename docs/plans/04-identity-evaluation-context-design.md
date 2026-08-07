# 04 — Strategy Identity vs. Evaluation Context (design decision)

**Status:** Design decision — needs operator sign-off before implementation. No
code ships with this note. Written 2026-08-07 after Gate 04-4 (`PromotionPipeline`,
PR #49) surfaced a P1 whose real cause is architectural, not local.

## 1. Why this note exists

Gate 04-4's Codex review found a legitimate P1 that cannot be fixed locally:
**`holdout_mode` cannot confirm the frozen winning strategy against the sealed
holdout window.** As a consequence 04-4 ships with `holdout_mode` **gated
fail-closed** (it raises), and the one-shot final-holdout confirmation (design
§4.0 step 8) is deferred until the decision in this note is made.

The P1 is the sharpest symptom of a theme that recurred across all four 04-4
review rounds (and echoes the 04-2 holdout-leak saga): **a value or identity
that actually ran diverges from the one that is checked, recorded, logged, or
confirmed.** Resolving the root cause here also clarifies several of those
symptoms.

## 2. The root cause

`config_hash` (the strategy's canonical identity — the composite PK of
`strategy_definitions`, cited by `strategy_trials`, `promotion_decisions`, and
the C6 versioning discipline) is computed by `strategy_registry/fingerprint.py`
over the **entire** strategy config, **excluding only** the runtime keys in
`_RUNTIME_KEYS = {"data_version"}`.

That means `config_hash` currently conflates two categorically different things:

| Category | Fields (today) | What it is |
|---|---|---|
| **Strategy identity** — *what the strategy IS* | signals, weights, `portfolio.*`, `execution.*`, `backtest.initial_capital` | The thing we are deciding whether to trust with capital. |
| **Evaluation context** — *how/when it was measured* | `data_version` (already excluded), **`backtest.start_date` / `backtest.end_date`** (currently INCLUDED in the hash) | The window/snapshot a measurement was taken over. Not part of "what the strategy is." |

`data_version` was already recognized as evaluation context and excluded from
identity (03A-5). **The backtest date window is the other evaluation-context
dimension, and it is still baked into identity.** That is precisely why holdout
confirmation is impossible: to evaluate the frozen winner over the holdout
window you must change its dates, which changes its hash — so you are no longer
evaluating the winner, you are evaluating a different definition. (The builder's
holdout tests sidestepped this with a separate holdout-dated definition, i.e.
they confirmed a *different* `config_hash`, which is the flaw Codex caught.)

## 3. How this theme showed up elsewhere in 04-4 (same shape)

- **R3 — config→MLflow mismatch:** the config *logged* omitted the `data_version`
  the *dispatched* copy carried → provenance-hash mismatch. (Fixed locally by
  logging the dispatched config.)
- **R4 — grid read/freeze TOCTOU:** the grid *read for the sweep* could diverge
  from the grid *frozen on the hypothesis*. (Fixed locally by re-reading the
  frozen grid.)
- **R2 — DSR sink-vs-source:** the DSR *persisted* diverged from the DSR the
  *consumers* used. (Fixed locally by normalizing at source.)

Each is "the thing used ≠ the thing recorded/checked." The holdout P1 is the
same shape at the identity layer, where a local patch is not available.

## 4. Proposed decision

**Draw the boundary explicitly: strategy identity = strategy parameters;
evaluation context = `data_version` + the evaluation window (backtest dates).**
`config_hash` covers identity only. A single frozen strategy identity can then
be *evaluated multiple times* — train/OOS during selection, then once over the
sealed holdout — each evaluation being a distinct **recorded measurement** over
its own window, all bound to the *same* identity. Holdout confirmation becomes
"the same frozen identity, measured over the holdout window," with the window
recorded as an evaluation parameter, not as part of the hash.

Concretely this means adding the backtest date window (`backtest.start_date`,
`backtest.end_date`) to the set of fields excluded from `config_hash` (joining
`data_version`), and threading the evaluation window as an explicit
per-measurement input rather than a config field that defines identity.

## 5. Blast radius — why this is the operator's call and needs its own slice

This changes what `config_hash` *means* repo-wide. Honest consequences to weigh:

- **Two backtests over different windows would share one `config_hash`.** Today
  they are distinct identities; after the change they are the same identity
  measured twice. Every consumer that assumed "one hash = one backtest" must be
  re-examined: `strategy_definitions` uniqueness, `strategy_runs`/`strategy_trials`
  keying, `promotion_decisions`, the backtest snapshot/manifest bundles (03A),
  and MLflow run identity.
- **C6** ("never modify a config used in a live session — version it") and **C7**
  ("record `data_version`") interact directly: the evaluation window would move
  from the versioned-config surface to the recorded-measurement surface. This
  must not weaken either rule — a live strategy's *identity* still can't change
  silently; only the recognition that a window is a measurement parameter changes.
- **Existing pinned snapshots/manifests and any already-recorded `config_hash`
  values** were computed with dates *included*. A migration/back-compat story is
  needed (recompute vs. dual-read vs. version the fingerprint algorithm) — the
  fingerprint is a root-of-trust, so this deserves the same care 03A gave content
  hashing.
- **The trial registry's window semantics** (`research_data_windows`,
  `strategy_trials.window ∈ {train_oos, holdout}`, the one-shot holdout seal) were
  designed assuming the window is separable from identity — so they are already
  aligned with this direction, which is corroborating evidence the boundary is
  the right one.

## 6. Options

1. **Adopt the boundary (recommended direction):** exclude the backtest window
   from `config_hash`; thread it as an explicit evaluation parameter. Implement as
   a dedicated slice (call it 04-4H or fold into 04-6) with a fingerprint-version
   + migration/back-compat plan. Then re-enable `holdout_mode` correctly (same
   frozen identity, holdout window, recorded as the one-shot confirmation).
2. **Linked dated-variant (no identity change):** keep dates in the hash; model
   holdout confirmation as a distinct `config_hash` explicitly *linked* to the
   winner (same `strategy_id` + same frozen strategy params, different window),
   recorded in `promotion_decisions`/evidence as "holdout confirmation of H,
   measured as H′." Smaller blast radius, but leaves `config_hash` semantically
   conflated and every future "same strategy, different window" need re-derives
   the linkage by hand.
3. **Status quo + no holdout confirmation:** keep `holdout_mode` gated
   indefinitely and drop the one-shot holdout step from the protocol. Not
   recommended — the sealed final holdout is a core anti-overfitting guarantee of
   Gate 04 (§4.0 step 8, §4.2).

## 7. Open questions for the operator

1. Which option (§6)? The recommendation is **Option 1** (adopt the boundary) —
   it is the only one that makes `config_hash` mean one coherent thing and makes
   holdout confirmation honest — but it has the largest blast radius and so is
   explicitly your architectural call.
2. If Option 1: is a **fingerprint algorithm version bump** (so old date-inclusive
   hashes remain valid under v1 and new date-excluded hashes are v2) acceptable,
   or do you want existing `config_hash` values recomputed? (Root-of-trust change —
   mirror 03A's care.)
3. Timing: implement the identity/holdout slice **before 04-6** (so the e2e proof
   exercises a real holdout confirmation), or land 04-5 (`validated` status) first
   and treat holdout as the last Gate-04 slice?

## 8. What ships now (independent of this decision)

04-4's **train/OOS promotion pipeline is complete and correct** and does not
depend on this decision (train/OOS evaluation matches the frozen config's own
dates). It ships with the two R4 P2s fixed (grid-candidate validation;
frozen-grid re-read) and `holdout_mode` gated fail-closed pointing here.

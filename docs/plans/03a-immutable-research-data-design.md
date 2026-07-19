# 03A — Immutable, PIT-Complete Research Data Design Plan

**Chat session ID:** `c30edbc0-c1e7-4e73-b97b-0835c073810a`

**Roadmap task:** Make research data immutable and PIT-complete (03A, P0, XL)
**Scope:** BUG-037, BUG-038, BUG-039, plus the eligibility-attribute gap left
open by 01B (`docs/plans/01b-research-validity-design.md` §1.3: "If a
historical version of a configured filter is not available, exclude it from
the baseline contract... do not substitute today's market cap, ADV, halted,
or bankruptcy state").
**Status:** Design plan — Phase 0 deliverable only. No code, migration, or
`bugs.md`/`Roadmap.md` change ships with this document. Implementation is
phased 03A-1 through 03A-5 (§5) and assigned to future builder sessions.

## Outcome and boundary

01B (delivered 2026-07-19) established the *minimum* effective-dated
membership and timing contract needed to reject survivorship bias and
same-close lookahead. It explicitly deferred three things to this gate:

1. eligibility filters (market cap, ADV, price, security type) as
   point-in-time attributes — 01B's own §1.3 says a filter without PIT data
   must be *excluded* from historical claims, not silently backfilled with
   today's value. No PIT eligibility store exists yet, so every strategy
   config filter beyond raw membership is currently unimplementable without
   violating that rule.
2. immutable, content-addressed research data bundles. 01B-3 (BUG-009) fixed
   *which* prices/actions a score may see; it did not fix that the bundle a
   score/backtest was computed from can still change bytes under a fixed
   name (BUG-038) or silently degrade on object-store failure (BUG-039).
3. lossless corporate-action preservation across the immutable-bundle
   boundary, including the same-date multi-action corruption in the
   in-memory adjustment-factor builder (BUG-037).

This plan's deliverable is a fail-closed, tamper-evident research data layer:
PIT eligibility alongside PIT membership, content-addressed immutable
snapshot bundles referenced by MLflow `data_version` (C7), corporate actions
preserved losslessly into those bundles, and object-store errors that abort
rather than degrade. It does **not** implement 03B (BUG-070 — splitting the
backtester's price series into raw-execution vs. analytic legs); §3 below
defines the *data contract* 03B will consume, not the backtester change
itself. It does not select or validate any strategy (Gate 04), and it does
not change the Wikipedia-sourced membership provider decided in
`docs/plans/01b2-constituent-source-contract.md`.

## Existing-state assessment

- `data/universe/runtime.py::load_universe_as_of` (01B/BUG-008) answers "was
  this ticker a member on this date" from `universe_membership`. It has no
  concept of market cap, ADV, price floor, or security type — those remain
  strategy-config-declared filters (`config/strategy/*.yaml`) evaluated, if
  at all, against whatever the *current* scoring run's live data happens to
  contain, which is exactly the substitution 01B §1.3 forbids.
- `scripts/pin_snapshot.py::pin_bundle` reads `daily_prices`, `alpha_scores`,
  `corporate_actions`, and a freshly-fetched benchmark series, writes each via
  `ParquetSnapshots.save_snapshot`, and builds a `DatasetManifest`
  (`backtesting/dataset_manifest.py`) keyed by the caller-supplied
  `snapshot_date` string. Re-running `pin_snapshot.py --snapshot-date
  2026-06-14` a second time silently overwrites every object at
  `snapshots/{data_type}/2026-06-14/data.parquet` and
  `manifests/2026-06-14/manifest.json` with new bytes (BUG-038); nothing
  refuses the overwrite and nothing prior recorded a content hash to detect
  it. `alpha_scores_sha256` on the manifest is the *only* existing tamper
  check, and it covers one of four bundled dataframes.
- `data/storage/parquet_snapshots.py::load_snapshot` converts any
  `minio.error.S3Error` to `FileNotFoundError` (BUG-039); the backtest loader
  (`backtesting/loader.py`) already treats a missing `corporate_actions`
  snapshot as *optional* and falls back to an empty DataFrame, which then
  silently produces `adj_factor=1.0` for the whole run — a transient MinIO
  auth/timeout/policy error becomes an unadjusted backtest with no error
  surfaced anywhere in the run's metadata.
- `data/normalization/corporate_actions.py::compute_adjustment_factors`
  accumulates a `multipliers` dict keyed by `ex_date` only (BUG-037); a
  same-day split + dividend (or split + spinoff) assigns twice to the same
  key and the second write silently discards the first action's multiplier.
  The `corporate_actions` table itself is *not* the bug — migration 001's
  `UniqueConstraint("ticker", "ex_date", "action_type")` already permits and
  correctly stores multiple same-date actions of different types; the loss
  happens only in this in-memory factor-accumulation step, which is shared by
  the cutoff-aware 01B-3 builders (`build_score_price_history_as_of`,
  `build_realized_total_return_as_of`) since both call
  `compute_adjustment_factors` after filtering.
- `data/research/models.py` (`ResearchMethodology`/`ResearchRun`, 01B §4)
  already gives every score/IC/backtest row a versioned methodology
  identity. This plan extends that identity to also carry the eligibility
  policy and the content-addressed bundle hash it was computed against,
  rather than inventing a parallel versioning scheme.

## Non-negotiable invariants

- A ticker is eligible for scoring/backtesting on date `d` only when it is a
  PIT member (01B) **and** every strategy-declared eligibility filter
  (market cap, ADV, price, security type, or any future filter) has a
  point-in-time value available as of `d`. A filter without a PIT source is
  never silently satisfied by a current value; the strategy config must omit
  it, or the run fails closed with a named missing-filter error.
- Two research runs that report the same `data_version` must be provably
  loading byte-identical inputs. Content addressing plus a load-time hash
  check are the enforcement mechanism, not documentation convention.
- Every corporate action recorded on a given `(ticker, ex_date)` — regardless
  of how many distinct `action_type` rows exist on that date — contributes to
  the adjustment factor. Silent multiplier loss is a data-correctness defect
  regardless of which caller (score cutoff, realized-return cutoff, or
  legacy full-history routine) triggers it.
- An object-store operation required for a research or backtest run either
  succeeds with verified content, or the run aborts with a typed error
  naming the missing/corrupt object. No code path may convert an
  infrastructure failure into an empty DataFrame, a default value, or an
  `adj_factor=1.0` fallback.
- Pre-03A snapshots/manifests remain readable (for audit and for backtests
  already logged against them) but are labeled `legacy_mutable` and may not
  be used as the `data_version` for a new research run once 03A ships.

## 1. Point-in-time eligibility-attribute contract

### 1.1 Scope and relationship to `universe_membership`

`universe_membership` (migration 009) answers index membership only.
Eligibility filters are a *second*, independent PIT axis: a ticker can be a
current S&P 500 member and still fail a strategy's `min_market_cap`,
`min_adv_usd`, `min_price`, or `security_type` filter on a given date. 01B
explicitly carved this out rather than letting `load_universe_as_of` grow
untracked scope. This plan adds it as its own table family so eligibility
policy can version independently of membership policy (a strategy can change
its ADV floor without triggering a new membership import).

### 1.2 Schema: `universe_eligibility_attributes`

An append-only, effective-dated fact table — one row per
`(universe_id, ticker, attribute_name, effective_start)` — mirroring the
half-open interval model of `universe_membership` rather than inventing a new
one:

| Field | Contract |
|---|---|
| `universe_id` | Same `universe_id` namespace as `universe_membership` (e.g. `sp500`). |
| `ticker` | Vendor-mapped ticker, joined through `universe_symbol_history` exactly like membership. |
| `attribute_name` | `market_cap_usd`, `adv_usd_20d`, `price_usd`, `security_type`, extensible without a schema change (`text`, not an enum, matching the config-driven filter design in `CLAUDE.md`). |
| `attribute_value_numeric` / `attribute_value_text` | One populated per row depending on `attribute_name`'s declared type; numeric filters (`market_cap_usd`, `adv_usd_20d`, `price_usd`) use the numeric column, `security_type` (`common_stock`, `adr`, `reit`, ...) uses the text column. A check constraint enforces exactly one is non-null per row. |
| `effective_start` / `effective_end` | Half-open interval, `effective_end` exclusive, `NULL` = still current as of the computing batch's `as_of_date`. For daily-recomputed numeric attributes (market cap, ADV, price) the practical interval is almost always exactly one trading session — see §1.4 on grain — so this is closer to a daily fact than a rare event, but the same interval shape lets `security_type` (which changes rarely) share the table and query path. |
| `computed_from` | Provenance: which upstream table/columns produced the value (e.g. `daily_prices.close * shares_outstanding`, `daily_prices.volume` 20-session mean). Required so a reviewer can audit whether a value used data available by `effective_start`'s close. |
| `source_data_asof` | The latest input date actually used (e.g. the `daily_prices.date` the close came from). Must be `<= effective_start`; a computation using future input data is a defect, not a valid row, and is rejected by an ingestion-side check, not merely documented. |
| `computation_batch_id` | FK to a new `universe_eligibility_batches` table (mirrors `universe_import_batches`): one row per nightly/backfill computation run, with `code_version` (git commit) and `computed_at`. Batches are append-only; correcting a bad computation publishes a **new** batch rather than mutating rows in place (same C3-style discipline as audit tables, extended here because eligibility values feed selection decisions that must stay attributable). |

No `EXCLUDE`/no-overlap constraint is imposed globally across
`attribute_name` (different attributes are independent series); a per-
`(universe_id, ticker, attribute_name, computation_batch_id)` no-overlap
`EXCLUDE` constraint mirrors migration 009's batch-scoped pattern.

### 1.3 Runtime interface

Add `load_eligibility_as_of(universe_id, as_of_date, filters:
dict[str, FilterSpec]) -> EligibilityResult` in `data/universe/runtime.py`
(same module as `load_universe_as_of`, not a new package — these two checks
are always evaluated together by callers). `FilterSpec` names the attribute,
comparison operator, and threshold as declared in the strategy YAML's
`universe.eligibility` block (a new, explicit config section — filters
silently assumed today are not acceptable per 01B §1.3). The result carries,
per ticker: pass/fail per filter, and a structured exclusion reason
(`missing_attribute`, `stale_attribute` when `source_data_asof` is older than
a configured staleness bound, or `below_threshold`/`wrong_type`). Combine
membership and eligibility into one `load_historical_universe_as_of` call
site used by score generation, IC validation, and backtesting so no caller
can apply one check without the other. A strategy config filter with no
matching `attribute_name` in the table (i.e., truly unavailable historically,
such as "halted" or "bankruptcy" state, which 01B §1.3 names explicitly as
currently unsourced) must fail the config-load step with a named
unsupported-filter error, not silently pass every ticker.

### 1.4 Grain, staleness, and restatement

Market cap, ADV, and price are derived from `daily_prices` (already PIT by
construction — `daily_prices.date <= as_of_date` is a real historical fact,
not a restated one, because RQIS ingests raw OHLCV and never overwrites a
historical row with a later-corrected value per existing ingestion design).
Computing these attributes as a **daily batch job** re-run for every trading
session (not "current value projected backward") keeps them PIT-safe by
construction: `market_cap_usd` on `d` uses `close[d] * shares_outstanding` as
known at `d`, not today's shares outstanding. `shares_outstanding` itself is
a fundamentals input with its own restatement risk; this plan requires the
ingestion source for `shares_outstanding` to carry a `filed_date`/`known_at`
comparable to the corporate-action `known_at` pattern (migration 011) before
`market_cap_usd` can be certified PIT — if no such source exists at
implementation time, `market_cap_usd` ships labeled `provisional_no_known_at`
and is excluded from any strategy's certified eligibility filter set until a
dated fundamentals source lands (tracked as an open question in §6, not
silently assumed away).

### 1.5 Acceptance tests

- A ticker whose `market_cap_usd` on `d` was below a strategy's threshold is
  excluded on `d` even though its *current* market cap exceeds the
  threshold, and vice versa.
- A ticker with no eligibility row for a required attribute on `d` is
  excluded with `missing_attribute`, never silently included.
- A strategy config referencing an attribute name with no historical source
  (e.g. `halted_flag`) fails config load with a named unsupported-filter
  error, not a silent no-op filter.
- `source_data_asof` newer than `effective_start` is rejected at ingestion
  (future-leak guard), mirroring 01B's membership `known_at` guard.
- Combined membership+eligibility exclusion reasons are distinguishable in
  the result (a reviewer can tell "not a member" from "member but illiquid"
  from "member but no eligibility data").

## 2. Immutable, content-addressed snapshot bundles

### 2.1 Content-addressed object layout

Replace the caller-supplied-date object key
(`snapshots/{data_type}/{YYYY-MM-DD}/data.parquet`, BUG-038) with a
content-addressed key computed from a **canonical logical content hash** of
the DataFrame, not from the serialized parquet bytes:

```
snapshots/{data_type}/sha256/{hash[0:2]}/{hash}/data.parquet
```

**Content identity is logical, not byte-level (PM amendment 1, option a).**
Parquet byte output is *not* deterministic across runs: the writer/library
version, footer metadata (e.g. `created_by` strings), compression details,
and incidental row ordering can all change the serialized bytes even when
the logical data is identical. A byte-derived key would therefore give two
pins of identical data two different keys, breaking the idempotency this
section promises (and §2.5's zero-new-writes acceptance test). Instead, the
hash is computed from a canonical serialization of the DataFrame's *values*:

1. sort rows by a per-data-type canonical key (e.g. `(score_date, ticker)`
   for `alpha_scores`, `(ticker, date)` for `daily_prices`/`benchmark`,
   `(ticker, ex_date, action_type)` for `corporate_actions`);
2. sort columns by name;
3. normalize dtypes (e.g. dates to ISO-8601 strings, numerics through one
   documented canonical string formatting) so pandas/pyarrow dtype drift
   between environments cannot change the hash of equal values;
4. hash the resulting canonical row-string stream with SHA-256 — the same
   sort-then-stringify-then-hash approach the existing
   `dataset_manifest._alpha_scores_hash` already uses, generalized to all
   four data types and all columns.

Parquet remains the **carrier format only**: the stored bytes are whatever
the current writer produces, but the object's identity (its key) is the
logical hash of what those bytes parse back to.

`ParquetSnapshots.save_snapshot` computes this canonical hash *before*
upload. If an object already exists at that hash's key, the write is skipped
(the content is already immutably stored; re-pinning the same logical data is
a safe no-op, not an error) — verified by downloading/parsing the existing
object and recomputing its canonical hash (or trusting a prior §2.3-style
verification recorded in the manifest registry), not merely "key exists," so
a partial prior upload cannot be trusted as complete. If the hash differs, it
is genuinely new logical content and gets its own key — nothing is ever
overwritten, because two different logical contents never map to the same
key. This removes the overwrite failure mode structurally rather than by
convention (an operator can no longer "re-run with the same date" and get
different data at the same path, because there is no date in the path).

**Trade-off (recorded deliberately):** this design provides *logical*-content
tamper-evidence, not raw-byte tamper-evidence — an attacker or bug that
rewrote the parquet bytes to a different-but-logically-equal encoding would
not be detected by the canonical hash. That is acceptable here: the research
guarantee RQIS needs is "same `data_version` ⇒ same logical inputs," and
byte-level rewrites that preserve logical equality cannot change a research
result. As defense-in-depth, `save_snapshot` additionally records the
SHA-256 of the uploaded bytes as a **secondary, informational**
`{data_type}_bytes_sha256` manifest field (never part of the key, never a
load-time gate) so out-of-band byte churn is at least observable.

A human-readable `snapshot_date` remains as **metadata on the manifest**, not
as part of the object key — reproducibility and browsability are served by
different mechanisms now instead of conflating them into one string.

### 2.2 Manifest becomes the immutable, hash-verified root

Extend `DatasetManifest` (`backtesting/dataset_manifest.py`) with:

| New field | Purpose |
|---|---|
| `content_sha256` per data type (already partial: `alpha_scores_sha256` covers one of four; extend to all four — `daily_prices_sha256`, `corporate_actions_sha256`, `benchmark_sha256` — using the §2.1 canonical logical-hash procedure, which generalizes the existing `_alpha_scores_hash` sort-then-hash approach) | Tamper-evidence at load time (§2.3), and the source of the content-addressed object key itself: the manifest's recorded canonical hash and the object key's hash are the *same value by construction* (both produced by the §2.1 procedure), and `save_manifest` checks that equality rather than merely asserting it. |
| `{data_type}_bytes_sha256` per data type (informational) | Secondary byte-level hash of the uploaded parquet carrier (§2.1 trade-off note). Never used for keys or load-time gating; recorded so out-of-band byte churn is observable. |
| `eligibility_batch_id`, `membership_import_batch_id` | Ties the bundle to the exact PIT membership/eligibility batches (§1) used to build it, so a bundle's universe inputs are as versioned as its price/score inputs. |
| `research_methodology_id` (nullable for non-research bundles) | FK to `data/research/models.py::ResearchMethodology`, unifying the two existing-but-separate versioning schemes (01B's methodology identity and the manifest's data identity) into one queryable link rather than two parallel systems a reviewer has to cross-reference by hand. |
| `manifest_content_sha256` | Hash of the manifest's own canonical JSON (excluding this field), computed after all other fields are set. This is the value used as MLflow `data_version` going forward — a single opaque, verifiable token rather than a mutable-looking date string. |

The manifest object key becomes
`manifests/{manifest_content_sha256}/manifest.json`. `save_manifest` refuses
to write if an object already exists at that path with *different* bytes
(should be structurally impossible since the key derives from the bytes, but
the check stays as defense against a hash-collision-shaped bug or manual
tampering, and fails loudly rather than assuming impossibility). A parallel
**pointer** object, `manifests/latest/{strategy_id}.json`, stores just
`{"manifest_content_sha256": "...", "created_at": "..."}` and *is* mutable —
operators need a "what's the newest pinned bundle for this strategy" lookup,
and that pointer is explicitly documented as mutable/advisory, never used
directly as a `data_version`.

### 2.3 Load-time tamper-evidence

`ParquetSnapshots.load_snapshot` (and a new
`load_snapshot_by_manifest(manifest, data_type)` that callers should prefer)
parses the downloaded parquet bytes into a DataFrame, recomputes the §2.1
**canonical logical hash** of the parsed frame, and compares it to the
manifest's recorded `{data_type}_sha256` before returning the DataFrame.
(The byte hash is *not* the gate — consistent with §2.1's logical-identity
decision — though a caller may optionally compare the informational
`{data_type}_bytes_sha256` for diagnostics when a logical mismatch is found.)
Mismatch raises a new `SnapshotIntegrityError` (distinct from
`FileNotFoundError` — see §4) rather than silently returning corrupted data.
This closes the gap where BUG-038's mutability made "the manifest says X but
the bytes now say Y" possible; content addressing prevents new corruption
from being written, and the load-time check catches corruption from any
out-of-band source (manual MinIO edit, bit rot, wrong bucket policy).

### 2.4 MLflow / C7 integration

`BacktestLogger.log_run()` and score-backfill call sites pass
`manifest_content_sha256` (or the full `manifests/{hash}/manifest.json` path,
for human navigability — both are logged as separate MLflow tags) as
`data_version`. Because the hash is a function of the canonical logical
content (§2.1), two runs sharing a `data_version` are now provably using
logically identical inputs — closing the specific gap the `DatasetManifest`
module's own docstring named (Codex finding #1: alpha scores/actions/
benchmark previously unversioned) one layer further down, at the
content-value level instead of the path-naming level.

### 2.5 Acceptance tests

- Re-running `pin_snapshot.py` with identical source data produces the same
  manifest hash and writes no new objects (idempotent no-op, verified via
  MinIO call-count assertions in tests, not just return-value equality) —
  **including** when the second run's parquet serialization differs
  byte-wise from the first (the test forces this with, e.g., a different
  writer metadata string or row insertion order), proving idempotency rests
  on the §2.1 canonical logical hash, not on accidental byte determinism.
- The canonical-hash procedure itself is deterministic across row order,
  column order, and equivalent dtype representations: shuffling a fixture
  frame's rows/columns or round-tripping it through parquet yields the same
  canonical hash, while changing any single value yields a different one.
- Re-running with even one changed row in any of the four bundled dataframes
  produces a different `manifest_content_sha256` and a new, additional
  object — the previous manifest and its objects remain byte-identical and
  loadable.
- Tampering with a stored object (test-only, simulated via a fake MinIO
  client) never returns a DataFrame: replacing the object with a parquet
  encoding of *different values* raises `SnapshotIntegrityError` (logical
  hash mismatch, §2.3), and corrupting raw bytes so the parquet no longer
  parses raises `SnapshotPartialReadError` (§4.1). A byte-different but
  logically-equal re-encoding loads successfully by design (§2.1 trade-off),
  with the informational bytes-hash discrepancy available for diagnostics.
- A manifest referencing an `eligibility_batch_id`/`membership_import_batch_id`
  that does not exist (or is not `published`/`validated` status) fails to
  build rather than pinning a bundle against an unpublished universe import.
- `BacktestLogger.log_run()` rejects a `data_version` that is not a
  recognized manifest-hash-shaped string once 03A ships (guards against a
  caller reverting to the legacy date-string convention silently).

## 3. Corporate-action preservation and the 03B data contract

### 3.1 Fixing BUG-037 without widening scope

`compute_adjustment_factors` (`data/normalization/corporate_actions.py`)
changes its internal accumulator from `dict[ex_date] = multiplier` (last
write wins) to `dict[ex_date] = product_of_all_multipliers_on_that_date`,
iterating all actions for a given `(ticker, ex_date)` — regardless of
`action_type` — and multiplying their individual adjustment contributions
together before the per-date entry is finalized. This is the single, narrow
fix BUG-037 calls for; it is shared automatically by both 01B-3 cutoff-aware
builders (`build_score_price_history_as_of`,
`build_realized_total_return_as_of`) and the legacy full-history routine,
since all three call `compute_adjustment_factors` as a shared dependency —
no separate fix is needed per caller.

**Same-date ordering/convention semantics (PM amendment 2).** "Product of
all multipliers on that date" is necessary but not sufficient: the
individual multipliers are only well-defined relative to a stated quoting
convention. A cash dividend's adjustment factor is
`(close - dividend_per_share) / close` relative to a reference close, and
when a split shares the ex-date, the dividend-per-share value may be quoted
**pre-split** or **post-split** depending on the data source — the two
conventions produce different (and non-interchangeable) per-action
multipliers even before any product is taken. The 03A-3 implementer is
therefore required to:

1. **document** which convention the ingested `corporate_actions` rows
   actually use for same-date split+dividend pairs (empirically verified
   against the current yfinance source's behavior, not assumed from its
   docs), recording the finding in the module docstring and in the
   `known_at_policy`-style provenance notes;
2. **normalize** all same-date actions to one declared convention (e.g.
   convert a pre-split-quoted dividend to its post-split equivalent, or vice
   versa) *before* computing per-action multipliers and multiplying them —
   the product is only valid over convention-consistent multipliers; and
3. **prove** the result with a same-date split+dividend fixture whose
   expected combined factor is **hand-computed under the stated convention**
   and asserted as a literal expected value in the test — not derived by
   running the two rows through the same code under test ("product of
   whatever the two rows contain" would pass even with a convention error
   baked in symmetrically).

If the source's convention cannot be determined empirically at
implementation time, 03A-3 must fail closed for same-date split+dividend
pairs (raise a named `AmbiguousSameDateActionError` rather than pick a
convention silently) and record the gap in `bugs.md`.

### 3.2 Raw preservation into immutable bundles

`corporate_actions` is already retained losslessly at the *database* level
(migration 001's `(ticker, ex_date, action_type)` uniqueness plus migration
011's `known_at`/`source_version` columns already give every row full PIT
provenance — there is no BUG-037-shaped loss at the storage layer, only in
the derived in-memory factor dict). §2's content-addressed bundle mechanism
extends that losslessness through the snapshot boundary: `pin_bundle` already
does a straight `SELECT * FROM corporate_actions` with no aggregation, so the
bundled `corporate_actions` dataframe is raw, one-row-per-action, and now
gets its own `corporate_actions_sha256` (§2.2) so a bundle's action set is
independently tamper-evident from the derived adjustment factors anyone
computes from it later. No adjustment factors are ever stored *in* the
bundle — they are always derived at read time by the caller-appropriate
builder, so a future BUG-037-class bug in the derivation logic can be fixed
and backtests re-derived without re-pinning data.

### 3.3 The data contract 03B will consume (not implement here)

BUG-070/row 03B is scoped separately: replacing `backtesting/loader.py`'s
single full-history adjusted series with a raw-execution series (fills, cash,
share accounting) plus the two cutoff-aware analytic builders. This plan's
job is only to guarantee 03B has what it needs once it starts:

- The content-addressed bundle's `daily_prices` dataframe is raw (unadjusted)
  OHLCV — already true today, unchanged by 03A.
- The bundle's `corporate_actions` dataframe is raw, complete (post-§3.1 fix),
  and independently hash-verified (§2.2/2.3) — so 03B's raw-execution leg and
  analytic leg can be derived from the *same* tamper-evident action set
  without either leg silently drifting from what was actually pinned.
- The manifest's `research_methodology_id` link (§2.2) gives 03B a place to
  record which score-cutoff and realized-return-cutoff policies it used, via
  the existing `ResearchMethodology` columns
  (`score_action_availability_policy`, `realized_return_action_availability_policy`)
  — no new methodology schema is needed for 03B specifically.

03A does **not** change `backtesting/loader.py`, does not decide the
raw-vs-analytic split's implementation details, and does not fix BUG-070.

### 3.4 Acceptance tests

- A synthetic same-date split+dividend fixture produces the correct combined
  adjustment factor (product of both convention-normalized multipliers), not
  either one alone, through all three callers (`compute_adjustment_factors`
  directly, and both 01B-3 cutoff-aware builders). The expected combined
  factor is a **hand-computed literal** under the convention documented per
  §3.1 (with the pre-/post-split dividend quoting explicitly stated in the
  fixture's comments), not a value derived by running the fixture through
  the code under test. A second fixture quoting the dividend under the
  *other* convention verifies the normalization step actually converts it
  (the two fixtures must converge to the same combined factor).
- A pinned bundle's `corporate_actions` row count and per-row values match a
  direct `SELECT * FROM corporate_actions` for the same ticker/date range
  exactly (no aggregation, no loss, value-identical under the §2.1 canonical
  hash check).
- Re-deriving adjustment factors from a previously-pinned bundle after the
  BUG-037 fix ships (i.e., re-running a fixed derivation over old, unchanged
  raw bundle bytes) produces the corrected factors without needing to
  re-pin — proving the raw-preservation design decouples the fix from data
  re-collection.

## 4. Fail-closed object-store handling

### 4.1 Error taxonomy

Replace the current blanket `except S3Error: raise FileNotFoundError` in
`ParquetSnapshots.load_snapshot` with a typed hierarchy in
`data/storage/parquet_snapshots.py`:

| Exception | Condition | Caller-visible meaning |
|---|---|---|
| `SnapshotNotFoundError` (renames/narrows current `FileNotFoundError`, kept as an alias for one deprecation cycle so existing `except FileNotFoundError` callers do not silently stop catching it) | MinIO's own not-found code (`NoSuchKey`/`NoSuchBucket`) only. | The object genuinely does not exist. This is the *only* condition allowed to be optional/absent for a caller that has explicitly opted into "this data type may be missing." |
| `SnapshotStoreUnavailableError` | Connection failure, timeout, DNS failure, TLS failure — MinIO not reachable at all. | Infrastructure is down; never treated as "no data," always aborts the run. |
| `SnapshotAccessDeniedError` | Auth/authorization failures (403-class `S3Error` codes). | Credentials/policy problem; never treated as "no data." |
| `SnapshotIntegrityError` (§2.3) | Downloaded content hash does not match the manifest's recorded hash. | Corruption/tampering; never treated as "no data." |
| `SnapshotPartialReadError` | Byte count read differs from the object's reported `Content-Length`, or the parquet footer fails to parse. | Truncated/corrupt transfer; never treated as "no data." |

Only `SnapshotNotFoundError` may ever be caught by a caller and converted
into "this optional data type is absent for this run." Every other error
propagates uncaught to the caller, which propagates to the script/DAG task,
which fails the run. This is the direct fix for BUG-039: today *every*
`S3Error` — including auth, timeout, and bucket-policy failures — collapses
into the same `FileNotFoundError` that the backtest loader's optional-actions
path silently accepts.

### 4.2 Where enforcement lives

- `data/storage/parquet_snapshots.py` is the single translation boundary from
  `minio.error.S3Error` to the typed hierarchy above; no other module should
  catch `S3Error` directly (a repository grep-based test enforces this, same
  pattern as 01B §3.2's `pct_change` inventory test).
- `backtesting/loader.py::load_from_snapshot` is updated so its
  "corporate_actions is optional" fallback catches only
  `SnapshotNotFoundError`. Any other exception aborts backtest construction.
  A backtest that genuinely intends to run without corporate-action data
  (e.g. a raw-price-only research probe) must pass an explicit
  `allow_missing_corporate_actions=True` flag rather than relying on
  incidental exception-type collapsing — the fallback becomes an opt-in, not
  a default.
- `scripts/paper_*` read paths and any future 03B execution-series loader
  follow the same rule: infra/auth/corruption errors always abort; only a
  confirmed absent object may become a documented default.

### 4.3 Acceptance tests

- A simulated MinIO timeout during snapshot load raises
  `SnapshotStoreUnavailableError` and the backtest run aborts with that
  error surfaced in its run record — not a silently empty corporate-actions
  frame.
- A simulated 403 raises `SnapshotAccessDeniedError` and aborts identically.
- A genuinely absent optional object (verified `NoSuchKey`) still allows the
  existing default-empty-frame behavior **only** when
  `allow_missing_corporate_actions=True` is explicitly passed; the default
  call site (used by 03B's future execution path) rejects a missing
  corporate-actions object outright, since "unadjusted backtest" must never
  be a silent default for the standard path (BUG-039's actual impact
  statement).
- A repository-wide test fails CI if any module outside
  `data/storage/parquet_snapshots.py` imports/catches `minio.error.S3Error`
  directly.

## 5. Migration/backfill plan and phased implementation breakdown

### 5.1 Existing-data disposition

- **Existing date-keyed snapshot objects** (`snapshots/{data_type}/{date}/...`)
  are left in place, untouched, and never migrated into the content-addressed
  layout automatically — a script cannot know whether two same-dated objects
  from different pin runs represent the "same" logical content without
  hashing them anyway, so this plan treats the legacy layout as a closed,
  read-only historical record rather than something to reconcile.
- **Existing manifests** (`manifests/{date}/manifest.json`) gain a
  `legacy_mutable: true` marker added by a one-time backfill script (not a
  code change to old objects, which stay immutable-in-place going forward —
  they simply stop being *producible* by new pins). A DB-side registry table
  `research_data_snapshots` (or a lightweight flag on `research_runs`, TBD at
  implementation time) records which historical `research_runs` rows point
  at a `legacy_mutable` manifest so a reviewer can see at a glance which
  past runs predate the tamper-evidence guarantee, without having to
  re-derive that from string-matching manifest paths.
- **No historical alpha_scores/backtests are recomputed** by this plan.
  Recompute-on-new-baseline is 01B §4's job (already executed once for
  01B/01B-3) and Gate 04's job for strategy selection; 03A only changes how
  *future* pins are stored and verified.
- **`universe_eligibility_attributes` backfill**: once the daily batch job
  (§1.4) exists, it is run once over the full historical `daily_prices`
  range to populate `market_cap_usd`/`adv_usd_20d`/`price_usd` for every
  session, gated on the `shares_outstanding`-with-`known_at` prerequisite
  named in §1.4. `security_type` is a much smaller one-time import (current
  security types are largely static; historical type changes, e.g. a REIT
  conversion, are rare enough to hand-curate per the same
  `universe_import_batches`-style provenance pattern as membership).

### 5.2 Phased breakdown

| Phase | Deliverable | Size | Depends on | Acceptance evidence |
|---|---|---|---|---|
| **03A-1** | Content-addressed snapshot store + immutable manifest (§2). `ParquetSnapshots.save_snapshot`/`load_snapshot` rewritten; `DatasetManifest` extended with per-data-type hashes and `manifest_content_sha256`; `pin_snapshot.py` updated to use content addressing; legacy-manifest backfill script (§5.1). | L | None (builds on existing `ParquetSnapshots`/`DatasetManifest` code, no schema for §1 needed yet) | §2.5 acceptance tests pass; a re-run of `pin_snapshot.py` against an unchanged DB produces zero new MinIO writes; existing legacy manifests still load and are flagged. |
| **03A-2** | Fail-closed object-store error taxonomy (§4). Typed exception hierarchy in `data/storage/parquet_snapshots.py`; `backtesting/loader.py` corporate-actions fallback narrowed to explicit opt-in; repo-wide `S3Error`-containment test. | M | 03A-1 (reuses the same module boundary; the integrity-mismatch exception from §2.3 is part of this same hierarchy). | §4.3 acceptance tests pass; simulated timeout/403/corruption tests all abort the run; grep-test confirms no other module catches `S3Error`. |
| **03A-3** | BUG-037 fix: same-date multi-action adjustment-factor accumulation (§3.1). Narrow change to `compute_adjustment_factors` plus the §3.1 same-date convention documentation/normalization requirement; regression tests for split+dividend (hand-computed expected factors under the documented convention, both quoting variants), split+spinoff, and three-action same-date fixtures across all three callers. | S | None (independent of 03A-1/2; can run in parallel). | §3.4's first acceptance test (including the hand-computed-literal and dual-convention fixtures) passes for all three callers; the ingested source's same-date quoting convention is documented per §3.1; existing 01B-3 cutoff-builder tests still pass unchanged. |
| **03A-4** | PIT eligibility-attribute schema and runtime (§1). New migration (`universe_eligibility_attributes`, `universe_eligibility_batches`); `load_eligibility_as_of`/`load_historical_universe_as_of` in `data/universe/runtime.py`; strategy-config `universe.eligibility` section parsing with fail-closed unsupported-filter rejection; daily batch job for `market_cap_usd`/`adv_usd_20d`/`price_usd`; resolution (or explicit `provisional_no_known_at` labeling) of the `shares_outstanding`-availability prerequisite. | XL | 03A-1 for the batch-job's provenance pattern to reuse the manifest/hash conventions consistently (not a hard technical dependency, but keeping conventions aligned reduces rework); otherwise independent of 03A-2/3. | §1.5 acceptance tests pass; full historical backfill of `market_cap_usd`/`adv_usd_20d`/`price_usd` completes with a coverage report (mirroring 01B-2's membership coverage report) showing per-date/per-attribute row counts and any `provisional_no_known_at` gaps. |
| **03A-5** | Manifest/methodology linkage + `data_version` cutover (§2.2, §2.4). Wire `eligibility_batch_id`/`membership_import_batch_id`/`research_methodology_id` into `DatasetManifest`; update `BacktestLogger.log_run()` and score-backfill call sites to pass `manifest_content_sha256` as `data_version`; reject legacy date-string `data_version` values for new runs. | M | 03A-1 (manifest schema), 03A-4 (batch IDs to link). | §2.5's last two acceptance tests pass; a new backtest run's MLflow record shows a hash-shaped `data_version` resolvable back to a manifest that itself resolves to the exact membership/eligibility batches used. |

Suggested builder sequencing: 03A-1, 03A-2, and 03A-3 can be assigned to
three parallel builders immediately (disjoint files: `parquet_snapshots.py`
+ `dataset_manifest.py` + `pin_snapshot.py` for -1; `parquet_snapshots.py`
error types + `backtesting/loader.py` for -2 — note -1 and -2 both touch
`parquet_snapshots.py`, so -2 should start from -1's merged output rather
than running fully in parallel branch-wise; and `data/normalization/
corporate_actions.py` alone for -3, fully independent). 03A-4 is the large,
independently schedulable item and can start in parallel with -1/-2/-3 since
it touches a disjoint file tree (`data/universe/*`, a new migration,
`config/strategy/*.yaml` parsing). 03A-5 is the integration phase and must
come last, after -1 and -4 both land.

## 6. Non-goals and open questions

### Non-goals (explicitly out of scope for 03A)

- Implementing BUG-070/03B's backtester raw-vs-analytic price series split.
  §3.3 defines the contract; the loader change itself is a separate roadmap
  row.
- Replacing the Wikipedia constituent provider or resolving its known
  count-drift (BUG-068) — that is a provider-swap decision already deferred
  to Gate 03/future commercial-source migration per
  `docs/plans/01b2-constituent-source-contract.md`'s "Provider-agnostic
  design" section.
- MinIO object-lock/WORM (write-once-read-many) bucket-policy enforcement at
  the infrastructure layer. Content addressing plus application-level
  refuse-to-overwrite-with-different-bytes (§2.1) gives practical
  immutability for this project's local Docker Compose MinIO deployment;
  true storage-layer WORM (S3 Object Lock or equivalent) is a defense-in-depth
  addition worth a future infra ticket, not a blocker for 03A, and is listed
  as an open question below.
- Recomputing or requalifying any existing strategy's backtest results.
- A general-purpose eligibility-filter DSL beyond the four named attributes
  (market cap, ADV, price, security type) from the roadmap description;
  `attribute_name` is a free-text column specifically so new attributes can
  be added by a future ingestion job without a schema migration, but this
  plan does not design ingestion for attributes beyond those four.
- Cross-referencing `universe_eligibility_attributes` against fundamentals
  restatement/point-in-time financial-statement data more broadly (that is
  the existing, separately-scoped fundamentals PIT question referenced in
  §1.4, not solved here beyond the `shares_outstanding` prerequisite named
  for `market_cap_usd`).

### Open questions requiring operator input before implementation

1. **`shares_outstanding` source and its `known_at` semantics.** §1.4 makes
   `market_cap_usd` PIT-certification conditional on a dated
   `shares_outstanding` source. Does one already exist in the current
   fundamentals ingestion path (`data/ingestion/...`), or does this plan need
   to scope a new one? If none exists at implementation time, is shipping
   `market_cap_usd` as `provisional_no_known_at` (excluded from certified
   strategy filters, visible but not usable for selection) acceptable, or
   should market-cap filtering be dropped from 03A's scope entirely until a
   dated source is available?
2. **ADV computation window and definition.** §1.2 assumes `adv_usd_20d` (20-
   session dollar-volume average) as the default ADV attribute; confirm this
   matches what existing/planned strategy configs actually intend to filter
   on, or whether multiple ADV windows need to coexist as separate
   `attribute_name` values from day one.
3. **`security_type` historical curation effort.** §5.1 proposes hand-curated
   historical security-type changes given expected low volume; confirm this
   is acceptable versus requiring a sourced provider (raises the same
   provider-decision process as `docs/plans/01b2-constituent-source-contract.md`
   did for membership).
4. **Legacy snapshot retention/cost.** §5.1 leaves all pre-03A date-keyed
   objects in place indefinitely. Is there a retention policy needed (e.g.
   archive-and-delete after N months) given local MinIO storage is
   presumably disk-bounded on the operator's machine, or is retention-
   forever acceptable at current data volumes?
5. **MinIO Object Lock / WORM.** Should a future infra ticket add
   storage-layer immutability (S3 Object Lock in compliance mode, or MinIO's
   equivalent) as defense-in-depth beyond 03A's application-level
   refuse-to-overwrite check, and if so, is that scheduled before or after
   Gate 04?
6. **`allow_missing_corporate_actions` default for non-backtest callers.**
   §4.2 makes the flag explicit for `backtesting/loader.py`. Should
   `scripts/validate_signal_ic.py`/other 01B-3 callers get the same explicit
   opt-in treatment as part of 03A-2, or is that better scoped as a
   03A-2 follow-up once the taxonomy lands and its blast radius across
   callers is easier to audit?

# 01B — Research-Validity Baseline Design Plan

**Codex session ID:** `019f577d-e183-7280-8f37-d8be8858d120`

**Roadmap task:** Repair research-validity baseline
**Scope:** BUG-008, BUG-009, and BUG-010
**Status:** Design plan — no research result is requalified by this document.

## Outcome and boundary

Gate 01B replaces three unsafe assumptions in historical research:

1. a current-membership universe is not treated as historical eligibility;
2. a score made with a session's close is not credited with that close's return;
3. a missing price is not silently forward-filled before an indicator computes a
   return.

The deliverable is a fail-closed research baseline used consistently by IC
validation, score backfills, and the event-driven backtester. It does not
deliver Gate 03's complete immutable object-store/snapshot architecture,
portfolio optimization semantics, paper-order execution, or a claim that any
strategy is valid.

Corporate-action observability is a P0 **research-certification** requirement
for this plan: it blocks a claim that historical scores are PIT-safe. It is not
a paper-runtime, broker-order, or immediate live-trading safety blocker. Its
implementation impact is limited to score/return data paths that use adjusted
prices; raw-price-only features must still declare and test that choice.

## Existing-state assessment

- `config/universe_loader.py` currently obtains current S&P 500 constituents
  from Wikipedia; it has no `as_of` API and returns an empty universe after a
  fetch failure.
- `signals/research/ic.py` computes a forward return as `close[t+h] / close[t]
  - 1`, while signals can use the close at `t`.
- `backtesting/engine/data_handler.py` already exposes scores only when
  `score_date < sim_date`; the IC implementation must adopt an equivalent
  execution convention rather than weaken this guard.
- `daily_prices` stores unadjusted OHLCV and corporate actions separately.
  Any return path used by research must state which adjusted-price construction
  it uses and must share that construction with backtests.
- `rg "pct_change\\(" signals -g "*.py"` currently finds 28 calls across the
  indicator library. Each is in scope unless an inventory records a justified
  exception.

## Non-negotiable invariants

- A ticker may participate on date `d` only when its membership interval covers
  `d`, all configured eligibility filters are evaluable as of `d`, and required
  data are present.
- Invalid/missing membership, price, adjustment, or eligibility data excludes
  the ticker/date and records a reason. It must never become a zero return or a
  silently retained prior value.
- The public API must use one explicit `as_of_date`/`score_date` and one
  documented execution-return convention. No caller may depend on an implicit
  current universe or same-date return.
- Historical outputs produced with the current-membership universe or
  same-close IC convention remain **provisional**. They may be retained for
  traceability but cannot be used for selection, promotion, or paper-trading
  qualification.

## 1. Point-in-time universe contract (BUG-008)

### 1.1 Canonical membership model

Introduce an effective-dated constituent record, initially represented by a
database table plus a source-normalized import artifact:

| Field | Contract |
|---|---|
| `universe_id` | Stable identifier, initially `sp500`. |
| `ticker` | Canonical, vendor-mapped tradable ticker; preserve vendor symbol separately when different. |
| `effective_start` | First session for which the security is eligible, inclusive. |
| `effective_end` | First session for which it is no longer eligible, exclusive; `NULL` means open-ended. |
| `source` / `source_record_id` | Vendor and source-row identity for audit/deduplication. |
| `announced_at` / `known_at` | When the change became knowable to this system; required for every historical-qualification record. |
| `source_version` / `ingested_at` | Version and acquisition time used by this import. |
| `reason` | Optional inclusion/removal reason; never used as a filter. |

Use half-open intervals: a row is eligible when
`effective_start <= as_of_date < COALESCE(effective_end, infinity)` **and** the
membership change was known before the configured market-calendar observation
cutoff for that session. Store the cutoff time zone and session calendar with
the import. A source that supplies only a date may be used only with a recorded,
conservative availability rule (no earlier than the next trading session); a
record with no knowable availability date/timestamp is rejected for historical
qualification. Enforce non-null start dates, valid ranges, and no overlapping
intervals for a `(universe_id, ticker)` pair. A security whose raw vendor symbol
changes must be joined through an explicit symbol-history mapping; do not
rewrite old ticks to the newest ticker symbol.

### 1.2 Source and ingestion decision

Before implementation, record the selected historical-constituents provider,
license/retention permission, coverage dates, change-effective semantics,
announcement/availability timestamps, market calendar/time zone, and
delisting/ticker-history coverage. The existing configuration names Polygon as
an intended future source, but the provider must not be assumed to supply every
required field without a recorded contract test.

The initial import must:

1. save the raw source response/file with a checksum and source version;
2. normalize symbols and membership intervals into a staging relation;
3. reject overlaps, inverted dates, unknown symbols, and coverage gaps;
4. derive and validate `known_at` using the approved source semantics, reject
   records that cannot meet the conservative availability rule;
5. publish only a complete validated import; and
6. retain a coverage report by date (constituent count, joins to prices,
   exclusions, and unresolved mappings).

This is the minimum provenance required for 01B. Gate 03 will make the full
research data bundle content-addressed and immutable; 01B must not pretend the
staging import alone satisfies that later gate.

### 1.3 Runtime interface and migration path

Add a single `load_universe_as_of(universe_id, as_of_date,
observation_cutoff, ...)` interface. It returns eligible tickers plus
structured exclusion reasons and fails closed when the requested date is
outside validated source coverage or the membership was not known by its
cutoff. Change
`airflow/dags/daily_data_pipeline.py`,
`airflow/dags/daily_signal_pipeline.py`, score generation, IC validation, and
historical backfill/operational ingestion callers (including
`data/ingestion/market/yfinance_client.py`) to use it where they operate
historically. Keep a separate,
explicit current-universe mode only for non-historical operational ingestion;
it must reject historical backtest/IC callers.

Historical eligibility is the intersection of membership, configurable filters
whose inputs are available as of the date, a valid adjusted price history, and
the strategy's declared rules. If a historical version of a configured filter
is not available, exclude it from the baseline contract and label the result;
do not substitute today's market cap, ADV, halted, or bankruptcy state.

### 1.4 Acceptance tests

- A removed constituent is included before, and excluded on/after, its
  effective end; an entrant behaves conversely.
- Adjacent intervals and remove-then-re-enter intervals are allowed; overlapping
  intervals, **global source/date coverage** gaps, future-announced changes,
  after-close announcements applied on the same session, and unknown-symbol
  mappings fail. A per-ticker absence is a valid non-membership interval, not a
  coverage failure.
- IC and backtest inputs exclude a ticker that has prices but lacks membership
  at that score date.
- A current-universe loader cannot be passed to historical IC/backtest code.
- A coverage report reconciles memberships, price joins, and exclusions for
  each requested date; an insufficient cross-section fails rather than emits an
  IC from a silently shrunken universe.

## 2. Signal-to-execution and return contract (BUG-009)

### 2.1 Recommended baseline convention

Adopt the convention already closest to the event-driven backtester:

| Event | Timestamp / rule |
|---|---|
| Score observation | Session `t` close; a score may use only data observable by that close. |
| Decision availability | After the close of `t`. |
| Earliest daily-bar execution | Session `t+1`; `score_date < execution_date` is mandatory. |
| Research entry reference | Total-return-adjusted close for `t+1`; actual fills/notional use the raw close under the baseline daily-bar model. |
| `h`-session evaluation return | `total_return_adjusted_close[t+1+h] / total_return_adjusted_close[t+1] - 1`; `h=1` is one full session after entry. |

The implementation must name these dates in outputs (`score_date`,
`entry_date`, `exit_date`) rather than label all of them `date`. A next-open
model is not a drop-in replacement: it requires an adjusted-open policy,
corporate-action treatment, execution-cost assumptions, and matching backtest
fills. Introduce it only through a separate approved design.

### 2.2 Price, corporate-action, and implementation contract

Do not replace tradable execution prices with adjusted prices blindly. Orders,
cash notional, and fills use raw tradable prices; split events must adjust
shares/cost basis and dividend events must adjust cash in the portfolio path.
IC and total-return analytics use a documented total-return-adjusted series
constructed from raw prices and `corporate_actions`. The implementation must
provide explicit interfaces for both series and label every caller's choice.

The parity test is economic, not merely numeric: a buy-and-hold fixture spanning
a split and dividend must produce the same total return from portfolio
accounting and the analytic adjusted-return series, while order notional remains
based on the raw fill price.

### 2.3 Corporate-action observability contract

Corporate actions are point-in-time inputs, not timeless adjustment factors.
Extend the action source/staging contract with the source action identity,
action effective/ex date, `announced_at`/`known_at`, source version, and market
calendar/time zone. `ingested_at` is not a substitute for historical
availability. An action without a defensible availability timestamp or approved
conservative date-only rule cannot be used to qualify historical score inputs.

Provide two separate, explicitly named interfaces:

- `build_score_price_history_as_of(..., score_cutoff)`: uses only actions known
  by the score observation cutoff. It is the only adjustment path permitted for
  a score feature at `t`.
- `build_realized_total_return_as_of(..., entry_date, exit_cutoff)`: uses only
  actions that occurred and were known by the return exit cutoff. It is used for
  realized IC returns and total-return analytics, never for raw fill notional.

Both interfaces must record the action-source version and availability policy
in their output metadata. The research methodology/run identity must include
both the score-action availability policy and the realized-return-action policy.
Do not reuse the current full-history backward adjustment routine for
historical score computation unless it accepts and enforces the score cutoff.

### 2.4 Required implementation changes

- Replace the same-close logic in `compute_forward_returns` with a return
  builder that accepts the explicit timing policy and emits entry/exit dates.
- Require `compute_ic_series` to merge scores with returns by score date,
  ticker, membership, and the declared timing-policy/version identifier.
- Use the analytic adjusted-return builder for IC and score diagnostics. Make
  the backtester use the raw execution series plus explicit corporate-action
  accounting, then compare its total-return valuation to the analytic series.
  Reject a requested return series when the required adjustment data cannot be
  constructed.
- Add the two as-of corporate-action builders and migrate score features,
  historical backfills, IC validation, and diagnostics to the appropriate one.
  Do not permit a caller to receive an unversioned, full-history adjusted series.
- Add the timing-policy identifier to IC summaries, `signal_ic_stats` records,
  validation reports, and MLflow/data-version metadata. If the current schema
  cannot hold it, add a migration rather than silently overloading a text field.
- Update `scripts/validate_signal_ic.py`, `scripts/audit_pit_safety.py`, and
  their tests so the audit verifies both strict score visibility and the actual
  entry/exit return alignment.

### 2.5 Acceptance tests

- A score using `close[t]` cannot receive any component of the `t` close-to-
  close return.
- For a hand-calculated series, one-session and multi-session returns use the
  documented entry and exit dates exactly.
- A holiday/missing ticker bar cannot cause row-position shifting across a
  different ticker's calendar. Each ticker's valid trading sessions determine
  its horizon, while membership remains checked on score, entry, and exit.
- A same-date score passed to the backtester is rejected/hidden, and IC uses
  the same strict-lag rule.
- Split/dividend fixtures prove portfolio total-return accounting matches the
  analytic adjusted-return policy without using adjusted prices for fills.
- A future split/dividend, including one back-adjusted by the legacy routine,
  cannot change a score feature at `t`; adding it must leave `score[t]`
  byte-for-byte/numerically identical.
- An action announced after the score cutoff is excluded from score inputs even
  if its effective date is earlier; a date-only source follows the approved
  conservative next-session rule.
- A realized return includes an eligible action by its exit cutoff and records
  the exact action-source/version and availability policy used.

## 3. Missing-data return policy (BUG-010)

### 3.1 Policy

Every price-derived return must be calculated with
`pct_change(fill_method=None)` or a central helper that is demonstrably
equivalent. Indicators must use the resulting `NaN` as missing information,
not replace it with zero or forward-filled data. Rolling metrics require the
declared number of **valid returns**, not merely the same number of calendar
rows or prices.

For a lookback of `N` returns, the default minimum is `N` valid returns in the
window and no gap where the individual indicator requires continuity. Each
indicator must document whether its statistic tolerates nonconsecutive valid
returns; the default is to reject a window spanning a gap. A lower robust
minimum requires a reason and a test. Cross-sectional scoring may omit
under-observed ticker/date values; it must report the resulting eligible count
and fail when the configured minimum cross-section is not met.

### 3.2 Implementation sequence

1. Generate and commit an inventory from every production price-return path:
   `signals`, `signals/research`, `backtesting`, and relevant scripts. Classify
   each call by module, window, current `min_periods`, output, and whether it
   uses a shared helper. This explicitly includes benchmark returns in
   `DataHandler`, not only indicators.
2. Add a narrow shared return helper in `signals/indicators/_price_utils.py`
   (or retain direct calls where clearer). It must validate positive finite
   prices, preserve gaps, and document return/observation counts.
3. Migrate the 28 current direct calls in small thematic batches. Do not use a
   blanket text rewrite: inspect indicators with price/volume combinations,
   beta benchmarks, and rolling ratio denominators independently.
4. Correct each rolling-window threshold so it counts valid returns after gaps.
   Record any intentional lower threshold in the inventory and unit test it.
5. Add a repository test that detects new direct `pct_change()` calls without
   `fill_method=None` or an approved helper across all production price-return
   paths, excluding only documented non-price cases.

### 3.3 Acceptance tests

- A gap between two valid prices yields `NaN` at the first post-gap return; it
  cannot yield a fabricated zero return.
- Insufficient contiguous/valid observations suppress the indicator value,
  reduce the eligible cross-section, and fail a configured minimum rather than
  imputing a score.
- Each migrated indicator retains its intended sign/scale on complete data.
- The static inventory test covers every production price-return calculation
  and gives a contributor an actionable file/line failure.

## 4. Recompute, invalidate, and retain evidence

1. Before recomputation, add versioned research identity. Create a
   `research_methodologies` record (universe import/version and availability
   policy; timing policy; score-action and realized-return-action availability
   policies; action-source version; return/adjustment policy; missing-data
   policy; and code/config hash) and a `research_runs` record referencing it plus the data
   version. Add `research_run_id` to `signal_ic_stats`, `factor_scores`, and
   `alpha_scores`; change their unique constraints/writers/readers so a new run
   cannot UPSERT over an old methodology. Backtests must persist the same
   methodology/run identity in their run metadata.
2. Mark existing records through a migrated legacy methodology/run as
   `legacy_provisional`; do not overwrite their metrics in place. Queries that
   need operational/current scores must select an explicitly approved active
   research run, not assume the newest row is valid.
3. After membership and timing contracts pass, generate a new data/timing
   version and recompute affected factor scores, alpha scores, IC statistics,
   diagnostics, and backtests from the supported historical start.
4. Preserve the old records and record the reason/version transition so metric
   changes are attributable to methodology rather than presented as improved
   performance.
5. Re-run the frozen holdout only once for each pre-specified new baseline. Do
   not tune factor parameters against the prior holdout after seeing the new
   result. A failed factor remains a valid negative result.
6. Retain the source coverage report, membership-import checksum, timing-policy
   version, missing-data inventory, test results, and before/after population
   counts with every validation run.

## 5. Implementation order and exit criteria

1. Record the constituent-source contract and create fixture data with adds,
   removals, ticker changes, price gaps, and corporate actions.
2. Implement the effective-dated membership schema/import/query API and its
   coverage/mapping tests.
3. Implement the explicit timing/adjusted-return contract, then make IC and
   the backtester consume it consistently.
4. Implement the missing-data helper/inventory and migrate indicators in
   reviewable batches.
5. Add versioned research identity/invalidation, recompute only after the above
   tests pass, and
   update runbooks/validation documentation with the new output meanings.

01B establishes only the minimum effective-dated membership and source evidence
needed to reject survivorship-biased research. Gate 03 remains responsible for
the broader immutable, content-addressed data bundle, full eligibility history,
corporate-action preservation, and object-store fail-closed behavior.

01B is delivered only when all BUG-008–010 acceptance tests pass, historical
research fails closed without validated membership/timing/price data, legacy
results are visibly provisional, and the new baseline has evidence for a
reproducible run. It remains distinct from Gate 03, which must later establish
fully immutable PIT-complete research data.

## Decisions requiring explicit approval before implementation

1. Historical constituent provider and license/retention terms.
2. Whether the recommended next-session-close baseline is accepted, or a more
   realistic next-open execution model is required (which expands the data and
   execution contract).
3. The historical availability of each strategy eligibility filter; filters
   without PIT data must be removed from historical claims or supplied with a
   compliant historical source.
4. Corporate-action source and its availability semantics, including the
   conservative rule for date-only split/dividend records. Without this, an
   adjusted-price score feature cannot be certified for historical research.

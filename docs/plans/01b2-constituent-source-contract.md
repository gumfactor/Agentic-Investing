# 01B-2 — Historical S&P 500 constituent source contract

**Roadmap task:** 01B-2 (`dev/R2-01B2-pit-universe`)
**Scope:** `docs/plans/01b-research-validity-design.md` §1.2 ("Source and ingestion
decision") for BUG-008.
**Status:** Recorded provider decision — implementation follows this contract.

This document is the recorded, operator-approved decision for the initial
historical-constituents provider required before any import runs. It exists so
a future contributor (or Gate 03's commercial-source migration) does not have
to reverse-engineer why the pipeline is shaped the way it is.

## Decision

Use **Wikipedia's "List of S&P 500 companies" page**
(`https://en.wikipedia.org/wiki/List_of_S%26P_500_companies`) as the initial
historical-constituents source for `universe_id="sp500"`.

The page currently exposes two tables relevant to this contract (verified
2026-07-16):

1. **Current constituents table** — one row per active constituent, including
   a `Date added` column giving the (real, ticker-specific) date each current
   member joined the index. This is real per-ticker addition data, not a
   synthetic placeholder.
2. **"Selected changes" table** — one row per addition/removal event, with an
   `Effective Date`, `Added` (ticker + security), `Removed` (ticker +
   security), and free-text `Reason` column. As of the 2026-07-16 fetch this
   table has 407 rows spanning 1976-07-01 through 2026-06-30.

Both tables are parsed by `data/universe/providers/wikipedia_sp500.py` into a
single reconstructed effective-dated interval history (see "Reconstruction
algorithm" below).

## Why Wikipedia, and why not wait for a commercial source

Polygon.io (the project's intended long-term market-data vendor) is deferred
to Phase 2+ per `CLAUDE.md`, and no commercial point-in-time constituent
feed is currently licensed. `PRD.md`/`CLAUDE.md` do not block this gate on a
paid subscription. Wikipedia's community-maintained history is:

- Free and immediately available (no license negotiation blocks Gate 01B).
- Cross-referenced against S&P press releases and news sources by editors,
  with inline citations for most rows (see the `Reason` column's footnote
  markers), which is materially better provenance than a hand-maintained CSV.
- Already the project's Phase 1 *current*-membership source
  (`config/universe_loader.py`), so this decision does not introduce a new
  unvetted dependency — it extends the existing one to be effective-dated.

This is **not** presented as research-grade, audited, point-in-time data.
See "Limitations" below. It is the minimum defensible source needed to reject
survivorship-biased research per the 01B exit criterion; Gate 03 remains
responsible for a fully immutable, audited constituent history and may
replace this provider with Polygon or another commercial source without a
schema change (see "Provider-agnostic design").

## License / retention

Wikipedia text content is licensed **CC BY-SA 4.0** (Creative Commons
Attribution-ShareAlike). This project:

- Retains the raw fetched HTML as a checksummed artifact under
  `data/vendor/sp500_wikipedia/<retrieved_at date>/` for audit and
  reproducibility (§1.2 step 1). The retained artifact is attributed to
  Wikipedia and its contributors in the artifact's `manifest.json`
  (`"attribution"` field) and in this document.
- Derives structured (ticker, date) facts from the page. Facts (dates,
  ticker symbols) are not copyrightable; the derived `universe_membership`
  rows are not a redistribution of Wikipedia's original text/prose and are
  not subject to CC BY-SA share-alike obligations. The raw HTML artifact
  itself, if ever redistributed outside this repository, must carry the CC
  BY-SA attribution recorded in its manifest.
- Does not use Wikipedia content for any purpose other than internal
  research/backtesting within this project.

## Coverage window and semantics

- **Coverage window:** `[1976-07-01, retrieved_at date]`. The "Selected
  changes" table's earliest row is 1976-07-01; membership before that date is
  **not certified** by this import (see "Left-censored intervals" below).
  `retrieved_at date` becomes the new coverage end each time the import is
  re-run; `load_universe_as_of` fails closed (raises `CoverageGapError`) for
  any `as_of_date` outside `[coverage_start, coverage_end]` of the latest
  published import batch.
- **`effective_start` / `effective_end` semantics:** half-open
  `[effective_start, effective_end)`, matching §1.1. `effective_end=NULL`
  means the constituent is still active as of the import's `coverage_end`.
- **`announced_at` / `known_at` semantics:** Wikipedia's tables give only a
  calendar date (`Effective Date` / `Date added`), never an announcement
  timestamp. Per §1.1's conservative date-only rule, this import:
  - Does not populate `announced_at` (the true S&P announcement date is
    usually a few days before the effective date and is not reliably present
    in either table; recording a guessed value would be worse than omitting
    it).
  - Sets `known_at` = the market-calendar session-close cutoff of the **next**
    trading session strictly after `effective_date` (see
    `data/universe/calendar.py::next_trading_session`). This guarantees a
    date-only record can never qualify a ticker for membership on its own
    effective-start session — see the acceptance test for "after-close
    announcements applied on the same session" in
    `data/tests/universe/test_acceptance_1_4.py`.
  - Rejects any staging row where the source supplies no parseable date at
    all (§1.2 step 4's "reject records that cannot meet the conservative
    availability rule").
  - Applies the same conservative rule to the REMOVAL side of a closed
    interval (`end_known_at` = next-session close after `effective_end`,
    Codex PR #34 review): a ticker remains eligible until its removal was
    knowable, because excluding it earlier would leak future removal
    information (removals correlate with declines, so early exclusion
    biases research upward). Left-censored interval *starts* are the one
    exception on the entry side (they are window boundaries, not real
    changes); their removals follow the normal rule.

## Reconstruction algorithm (real data, no fabrication)

The current-constituents table gives an authoritative `effective_start` for
every ticker currently in the index (its `Date added` column). The
"Selected changes" table gives `(effective_date, added_ticker, removed_ticker,
reason)` events. The importer (`data/universe/import_pipeline.py`) merges
these into intervals as follows:

1. Every row in the current-constituents table becomes an open interval:
   `(ticker, effective_start=Date added, effective_end=NULL)`.
2. Every "Selected changes" row is classified as either a **ticker rename**
   (same underlying entity changed its vendor symbol — detected by a
   `reason` string matching `/ticker symbol/i` with both an `Added` and
   `Removed` ticker on the same effective date) or a **membership change**.
   - Ticker renames produce one `universe_symbol_history` row
     (`old_ticker`, `new_ticker`, `effective_date`) and do **not** by
     themselves open/close a membership interval — the underlying entity's
     membership is treated as continuous across the rename event as long as
     both symbols already have adjacent intervals; the parser closes the old
     ticker's interval and opens the new ticker's interval at the rename
     date so `daily_prices` joins (which are keyed by raw vendor ticker,
     never rewritten — §1.1) still resolve correctly on each side.
   - Membership changes with a `Removed` ticker not otherwise present as a
     still-open current-constituent interval close the most recent open
     interval for that ticker (or, if no earlier `Added` event for that
     ticker exists inside the coverage window, open a **left-censored**
     interval starting at the coverage window start — see below).
   - Membership changes with an `Added` ticker open a new interval starting
     at `effective_date`.
3. **Left-censored intervals:** a ticker that was removed inside the coverage
   window but never appears as `Added` inside the window (i.e., it was
   already a member when Wikipedia's "Selected changes" table begins in
   1976) gets `effective_start = coverage_start` (1976-07-01) with
   `reason="left_censored_pre_coverage_window"`. These rows are flagged in
   the coverage report (`unresolved_left_censored_count`) so a caller can see
   how many intervals have an approximate rather than exact start. Queries
   for `as_of_date` on or after the interval's real (unknown) true start are
   still correct; only the exact historical `effective_start` value itself is
   approximate for these specific tickers.

### Additional reconstruction rules discovered during real-data verification

- **Same-symbol replacement:** a change row whose `Added` and `Removed`
  tickers are identical (real example: 2011-12-12 "Nicor acquired by AGL,
  which retained the GAS ticker") is treated as continuous membership under
  that symbol — no interval boundary is created, and a warning is recorded.
  Producing an add+remove pair would create an empty `[d, d)` interval.
- **Removal on/before `coverage_start`:** gives no evidence of in-window
  membership; no left-censored interval is created (a warning is recorded).

### Recorded operator exclusions (2026-07-17 verification import)

Three tickers are excluded via the importer's `--exclude-tickers` escape
hatch because Wikipedia reuses the same symbol for two different companies in
different eras, which a symbol-keyed reconstruction cannot disambiguate
without fabricating an interval start:

| Ticker | Collision |
|---|---|
| `AN` | Amoco (removed 1998-12-11) vs. AutoNation (removed 2017-08-08; no in-window addition row) |
| `SUN` | SunAmerica (removed 1998-12-11) vs. Sunoco (removed 2012-10-10; no in-window addition row) |
| `AGN` | Allergan v1 (removed 2015-03-23) vs. Allergan v2 (removed 2020-05-12; no in-window addition row) |

Excluded tickers receive **no** membership intervals: for historical queries
they are never eligible, which is the fail-closed direction (it can only
shrink the historical universe, never inflate it with unverifiable
membership). A commercial provider with entity-level identifiers can restore
them at Gate 03.

### Import verification (2026-07-17)

The full pipeline (fetch → persist → stage → validate → publish) was run
against the live page on 2026-07-17 and published cleanly with the three
exclusions above:

- Raw artifact: `data/vendor/wikipedia_sp500/2026-07-17/raw.html`
  (sha256 `3395c346fba67789d1e0170d919c6d74e42922d66001755a50ad691cc647d170`,
  checked in with its `manifest.json` so the import is reproducible offline
  via `--snapshot`).
- 503 current-constituent rows, 407 change events parsed; 891 membership
  intervals, 6 symbol-history rows published; 241 left-censored intervals.
  (Counts updated after the Codex PR #34 fix that preserves the
  non-excluded side of AN/SUN/AGN change events; the pre-fix import
  produced 890 intervals with 245 left-censored.)
- Coverage-report member counts: 500 (2010-01-04), 520 (2023-06-01);
  pre-fix values were 417 (2000-01-03), 502 (2010-01-04), 522 (2020-01-02),
  519 (2023-06-01), 518 (2026-06-30).

The count drift versus the true ~503-505 constituent count (under-counting in
2000, over-counting ~3% in 2020+) is the expected artifact of Wikipedia's
"Selected changes" table being incomplete in earlier decades: left-censored
intervals whose true start is later than `coverage_start` inflate later
dates, and missing early change rows deflate earlier dates. This inaccuracy
adds or retains names — it does not re-introduce the survivorship-bias
direction (systematically excluding removed losers) that BUG-008 targets —
and it is bounded and visible in the coverage report. The project's
supported backtest window (2022-07-11 → 2024-12-31) sits in the
best-covered recent era.

## Limitations (recorded per §1.2 and the operator directive to never overstate coverage)

- **Community-maintained, not a licensed audit feed.** Wikipedia edits can
  contain transcription errors, be briefly vandalized, or lag a real S&P
  Dow Jones Indices announcement by hours to days. The `source` column on
  every imported row is `"wikipedia_sp500"` so any downstream consumer can
  filter/distrust it explicitly, and MLflow/run metadata (01B-3/§4 scope)
  will carry the same provenance tag.
- **No announcement timestamp.** As stated above, `announced_at` is never
  populated by this provider; only the conservative `known_at` derived from
  the effective date is available. A future provider (e.g. Polygon, or a
  press-release scrape) could supply a tighter `announced_at` without a
  schema change (the column already exists and accepts `NULL`).
- **Ticker-rename detection is heuristic.** The `/ticker symbol/i` regex on
  the free-text `Reason` column will miss renames phrased unusually and will
  not fabricate a rename it cannot detect — such rows are instead imported as
  an ordinary removal + addition pair (safe default: no artificial
  continuity is invented).
- **Pre-1976 membership is not certified.** `load_universe_as_of` fails
  closed for any `as_of_date` before 1976-07-01. The project's current
  supported backtest window (`2022-07-11` through `2024-12-31`, per
  `CLAUDE.md`) is well inside the certified coverage window.
- **Coverage window advances only when the import is manually re-run.**
  Unlike the old `config/universe_loader.py::load_universe()` (which fetches
  live on every call and fails open to an empty list), this import is a
  deliberate operator step (`python -m scripts.import_universe_membership`)
  that must be re-run periodically to keep `coverage_end` current. Historical
  queries for dates after the last import's `coverage_end` fail closed
  (`CoverageGapError`), they never silently reuse stale membership.

## Provider-agnostic design (for a future Polygon/commercial swap at Gate 03)

- `data/universe/providers/base.py` defines `ConstituentProvider`, a small
  protocol (`provider_name`, `fetch() -> RawSnapshot`,
  `parse(raw) -> ParsedConstituentData`) that any future provider
  implements. `WikipediaSP500Provider` and the test-only `FixtureSP500Provider`
  are the only two implementations today.
- The import pipeline (`data/universe/import_pipeline.py`), the DB schema
  (`infra/db/migrations/versions/009_universe_membership.py`,
  `data/universe/models.py`), and the runtime query API
  (`data/universe/runtime.py::load_universe_as_of`) never reference
  "Wikipedia" by name — they operate on the provider-neutral
  `RawSnapshot` / `ParsedConstituentData` / `universe_membership` row shapes.
  Swapping in a Polygon-backed provider means writing one new
  `ConstituentProvider` implementation; no migration, no runtime API change.
- The `source` / `source_version` columns on every row exist precisely so
  multiple providers' imports can coexist in the same table (e.g. during a
  Gate 03 migration/reconciliation period) without ambiguity about which
  provider produced which row.

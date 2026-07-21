"""PIT eligibility-attribute batch job (03A-4b, Phase B of BUG-078).

Populates ``universe_eligibility_attributes`` rows for the three PIT-safe
attributes certified in 03A-4a (Phase A, ``data/universe/eligibility_config.py``):

- ``adv_usd_20d`` / ``price_usd`` -- computed daily from ``daily_prices``
  (design doc §1.4: PIT-safe by construction, since ``daily_prices.date <=
  as_of_date`` is a real historical fact and RQIS never overwrites a
  historical row with a later-corrected value). See
  :func:`compute_price_eligibility_rows` / :func:`write_price_eligibility_batch`.
- ``security_type`` -- hand-curated historical changes plus a default
  classification for every other tracked member, with
  ``universe_import_batches``-style provenance (operator decision,
  design doc §5.1/§6 item 3). See :func:`build_security_type_rows` /
  :func:`write_security_type_batch`.

``market_cap_usd`` is permanently out of scope for this module (operator
decision: yfinance has no filing-dated shares-outstanding source, design doc
§1.4/§6 item 1) -- do not add it here without a new binding operator
decision recorded in the design doc.

Also provides :func:`eligibility_coverage_report`, mirroring
``data/universe/import_pipeline.py::coverage_report`` for the eligibility
axis (design doc §5.2's 03A-4 acceptance evidence: "a coverage report...
showing per-date/per-attribute row counts and any provisional_no_known_at
gaps").
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union

import pandas as pd
import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from data.universe.calendar import next_trading_session, session_close_cutoff
from data.universe.models import (
    Base,
    UniverseEligibilityAttribute,
    UniverseEligibilityBatch,
    UniverseMembership,
    UniverseImportBatch,
)

logger = structlog.get_logger(__name__)

# Attributes this module is certified to populate. market_cap_usd is
# deliberately absent (see module docstring).
PRICE_ATTRIBUTE_NAMES = ("adv_usd_20d", "price_usd")
SECURITY_TYPE_ATTRIBUTE_NAME = "security_type"
DEFAULT_ADV_WINDOW = 20
DEFAULT_SECURITY_TYPE = "CS"  # common stock -- the overwhelming default (§5.1)

# Explicitly out-of-scope attribute, named so the coverage report can report
# it as a deliberate exclusion rather than a silent gap (design doc §1.4/§6).
EXCLUDED_ATTRIBUTES = {
    "market_cap_usd": (
        "excluded from the certified eligibility set: yfinance has no "
        "filing-dated (known_at-comparable) shares-outstanding source "
        "(design doc §1.4/§6 item 1); not provisional_no_known_at, "
        "dropped entirely per binding operator decision"
    ),
}


class EligibilityBatchError(Exception):
    """Base class for eligibility batch-job failures."""


class EmptyBatchError(EligibilityBatchError):
    """A batch computation produced zero rows.

    Fails closed rather than writing a ``universe_eligibility_batches`` row
    with no attribute rows behind it -- an empty result almost always means
    the caller passed an empty/misfiltered price frame or an out-of-range
    date window, not a legitimate "nothing to report" outcome.
    """


# ─── §1: adv_usd_20d / price_usd daily batch job ──────────────────────────────


def compute_price_eligibility_rows(
    prices: pd.DataFrame,
    *,
    start: date,
    end: date,
    adv_window: int = DEFAULT_ADV_WINDOW,
) -> list[dict]:
    """Pure computation: PIT-safe ``adv_usd_20d``/``price_usd`` attribute rows.

    Args:
        prices: long-format frame with ``ticker``, ``date``, ``close``,
            ``volume`` columns (as read from ``daily_prices``). Must include
            at least ``adv_window - 1`` trailing sessions before ``start``
            per ticker for ADV to be computable at the start of the range;
            tickers/dates without a full trailing window simply get no
            ``adv_usd_20d`` row (fail-closed: no partial-window average is
            ever emitted), not a caller error.
        start: first ``effective_start`` (inclusive) to emit rows for.
        end: last ``effective_start`` (inclusive) to emit rows for.
        adv_window: trailing session count for the dollar-volume average
            (design doc §6 operator answer 2: ``adv_usd_20d`` is the single
            confirmed default; no other windows in 03A).

    Returns:
        Row dicts with every column ``UniverseEligibilityAttribute`` needs
        EXCEPT ``universe_id``/``computation_batch_id``/``created_at``,
        which the caller (:func:`write_price_eligibility_batch`) fills in at
        write time so this function stays a pure, DB-free computation
        (testable without any engine).

    Grain (design doc §1.4): one row per (ticker, attribute, trading
    session) -- "current value projected backward" is exactly what this
    function does NOT do. Each ticker's own qualifying dates are chained
    into consecutive one-session ``[effective_start, effective_end)``
    intervals, EVERY one closing at the next trading session (see
    :func:`_chain_intervals`'s docstring for why the originally-open last
    row was itself a defect, Codex P1 round 2, PR #42) -- a date with no
    covering row from any batch reports ``missing_attribute`` rather than
    silently inheriting an indefinitely "current" stale value.
    """
    if prices.empty:
        return []

    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["dollar_volume"] = df["close"] * df["volume"]
    df["adv_usd_20d"] = df.groupby("ticker")["dollar_volume"].transform(
        lambda s: s.rolling(adv_window, min_periods=adv_window).mean()
    )

    in_range = df[(df["date"] >= start) & (df["date"] <= end)]

    rows: list[dict] = []
    for ticker, group in in_range.groupby("ticker"):
        group = group.sort_values("date")

        price_rows = group.loc[group["close"].notna(), ["date", "close"]]
        rows.extend(
            _chain_intervals(
                ticker=ticker,
                attribute_name="price_usd",
                dated_values=list(zip(price_rows["date"], price_rows["close"])),
                computed_from="daily_prices.close",
            )
        )

        adv_rows = group.loc[group["adv_usd_20d"].notna(), ["date", "adv_usd_20d"]]
        rows.extend(
            _chain_intervals(
                ticker=ticker,
                attribute_name="adv_usd_20d",
                dated_values=list(zip(adv_rows["date"], adv_rows["adv_usd_20d"])),
                computed_from=(
                    f"daily_prices.close*volume, {adv_window}-session trailing mean"
                ),
            )
        )

    return rows


def _chain_intervals(
    *, ticker: str, attribute_name: str, dated_values: list[tuple[date, float]], computed_from: str
) -> list[dict]:
    """Turn a ticker's sorted (date, value) series into chained one-session
    half-open intervals -- EVERY row closes at the next trading session,
    never left open-ended.

    Codex P1 fix, round 1 (03A-4b PR #42 review): a row's ``effective_end``
    is the actual NEXT TRADING SESSION after its date
    (``data/universe/calendar.py``'s NYSE-approximate calendar), NOT the date
    of the next entry in ``dated_values``. The two coincide when the input is
    contiguous, but if a session's raw observation is missing (an ingestion
    gap -- e.g. close/volume absent for one trading day), the previous
    entry's row must stop exactly at that missing session rather than
    silently stretching forward to whichever later date happens to have the
    next valid value. Silently extending a stale value across an unobserved
    session is exactly the substitution defect this repo already fixed for
    membership (BUG-008) and object-store errors (BUG-039); leaving the gap
    session uncovered means :class:`~data.universe.runtime.PITEligibilityLookup`
    correctly reports ``missing_attribute`` for it instead of inheriting the
    prior session's value.

    Codex P1 fix, round 2 (PR #42 second review): the ORIGINAL design left
    the LAST row of a batch's chain open-ended (``effective_end=None``,
    "still current as of this computation batch"). That interacted badly
    with :class:`PITEligibilityLookup`'s latest-``computed_at``-wins
    correction rule: a small corrective/backfill batch covering only a
    finite subrange (e.g. re-running Jan 2-Jan 5 to fix one bad value) would
    leave Jan 5's row open, so its later ``computed_at`` would make it
    outrank an OLDER full-history batch's rows for every date after Jan 5
    too -- silently overriding the entire subsequent history with one stale
    corrective value, even though the corrective batch never computed
    anything past Jan 5. Every row now always closes at
    ``next_trading_session(d)`` regardless of position in the batch; a date
    that genuinely has no covering row from ANY batch (e.g. tomorrow,
    before tomorrow's daily job has run) correctly reports
    ``missing_attribute`` rather than silently inheriting an indefinitely
    "current" stale value -- consistent with this module's fail-closed
    design throughout.
    """
    rows: list[dict] = []
    for d, value in dated_values:
        rows.append(
            dict(
                ticker=ticker,
                attribute_name=attribute_name,
                attribute_value_numeric=float(value),
                attribute_value_text=None,
                effective_start=d,
                effective_end=next_trading_session(d),
                computed_from=computed_from,
                source_data_asof=d,
            )
        )
    return rows


@dataclass(frozen=True)
class EligibilityBatchWriteResult:
    batch_id: int
    n_rows_written: int
    n_tickers: int
    n_dates: int
    attribute_names: tuple[str, ...]


def write_price_eligibility_batch(
    engine: Union[Engine, str],
    universe_id: str,
    prices: pd.DataFrame,
    *,
    start: date,
    end: date,
    code_version: str,
    adv_window: int = DEFAULT_ADV_WINDOW,
    computed_at: Optional[datetime] = None,
    notes: Optional[str] = None,
) -> EligibilityBatchWriteResult:
    """Compute and persist one ``adv_usd_20d``/``price_usd`` computation batch.

    Creates exactly one new ``universe_eligibility_batches`` row (append-only
    -- a correction is a NEW batch, never a mutation of an existing one,
    §1.2) and its attribute rows in the same transaction. Fails closed
    (:class:`EmptyBatchError`) rather than writing an empty batch header.
    """
    if isinstance(engine, str):
        engine = create_engine(engine)
    Base.metadata.create_all(engine)

    rows = compute_price_eligibility_rows(prices, start=start, end=end, adv_window=adv_window)
    if not rows:
        raise EmptyBatchError(
            f"compute_price_eligibility_rows produced zero rows for "
            f"universe_id={universe_id!r}, start={start}, end={end}. Check that "
            "`prices` actually covers this range and includes at least "
            f"{adv_window - 1} trailing sessions before `start`."
        )

    computed_at = computed_at or datetime.now(timezone.utc)
    with Session(engine) as session:
        batch = UniverseEligibilityBatch(
            universe_id=universe_id,
            code_version=code_version,
            computed_at=computed_at,
            n_attribute_rows=len(rows),
            notes=notes,
            created_at=computed_at,
        )
        session.add(batch)
        session.flush()
        for row in rows:
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=universe_id,
                    computation_batch_id=batch.id,
                    created_at=computed_at,
                    **row,
                )
            )
        session.commit()
        batch_id = batch.id

    logger.info(
        "price_eligibility_batch_written",
        universe_id=universe_id,
        batch_id=batch_id,
        n_rows=len(rows),
        start=str(start),
        end=str(end),
    )
    return EligibilityBatchWriteResult(
        batch_id=batch_id,
        n_rows_written=len(rows),
        n_tickers=len({r["ticker"] for r in rows}),
        n_dates=len({r["effective_start"] for r in rows}),
        attribute_names=tuple(sorted({r["attribute_name"] for r in rows})),
    )


# ─── §2: security_type hand-curated backfill ──────────────────────────────────
#
# Codex P2 fix (03A-4b PR #42 review): curated-vs-default provenance is
# discriminated by a fixed, code-owned tag prefix on `computed_from` --
# "curated:"/"default:" -- rather than by prefix-matching human-readable
# prose. There is no dedicated schema column for this (adding one is a new
# Alembic migration, out of proportion for this fix); the tag is the first
# ":"-delimited token and is never influenced by an operator-supplied curation
# note, which is appended only after the tag.

_CURATED_SOURCE_TAG = "curated"
_DEFAULT_SOURCE_TAG = "default"


def _security_type_computed_from(source_tag: str, detail: str) -> str:
    return f"{source_tag}:{detail}"


def _is_curated_security_type_row(computed_from: str) -> bool:
    return computed_from.split(":", 1)[0] == _CURATED_SOURCE_TAG


def _removal_lag_boundary_date(end_known_at: datetime) -> date:
    """The first calendar date ``D`` such that
    ``session_close_cutoff(D) >= end_known_at`` -- the exact half-open
    ``effective_end`` boundary matching
    :class:`~data.universe.runtime.PITUniverseLookup`'s per-date,
    per-cutoff eligibility check (``end_known_at > session_close_cutoff(as_of_date)``
    keeps a not-yet-knowably-removed ticker eligible).

    Codex P2 fix (PR #42 second review, round 2): using ``end_known_at.date()``
    directly is only correct when ``end_known_at``'s time-of-day is at or
    before the session-close cutoff hour on its own date -- true for this
    repo's only current source of ``end_known_at``
    (``conservative_known_at_for_date_only_source``, which sets it to
    exactly ``session_close_cutoff(next_trading_session(...))``). A
    provider-supplied removal announcement landing AFTER that date's own
    cutoff (e.g. an intra-day timestamp past 21:00 UTC) would otherwise
    still leave the ticker knowably-member on ``end_known_at.date()``
    itself per ``PITUniverseLookup``, while the naive boundary already
    stopped covering that date -- reintroducing the same divergence one day
    later. ``session_close_cutoff`` is defined for any calendar date (not
    only trading sessions), and a fixed cutoff HOUR each day means at most
    one day's advance is ever needed: any timestamp on date ``D`` is
    strictly earlier than ``session_close_cutoff(D + 1 day)``.
    """
    boundary_date = end_known_at.date()
    if session_close_cutoff(boundary_date) < end_known_at:
        boundary_date = boundary_date + timedelta(days=1)
    return boundary_date


@dataclass(frozen=True)
class SecurityTypeCurationEntry:
    """One hand-curated, verified historical ``security_type`` fact for a
    ticker (design doc §5.1/§6 item 3: "hand-curated historical changes
    approved, with universe_import_batches-style provenance").

    ``effective_end=None`` means the classification is still current as of
    this curation batch. A ticker may have multiple entries (e.g. a REIT
    conversion) as long as they do not overlap -- overlap is rejected at
    write time, not silently accepted.
    """

    ticker: str
    security_type: str
    effective_start: date
    effective_end: Optional[date] = None
    note: str = ""


class SecurityTypeCurationError(EligibilityBatchError):
    """A curation entry is malformed or overlaps another entry for the same
    ticker."""


def load_membership_intervals(
    engine: Engine, universe_id: str
) -> dict[str, list[tuple[date, Optional[date], Optional[datetime]]]]:
    """Ticker -> membership intervals from the latest PUBLISHED import batch
    (same batch :class:`~data.universe.runtime.PITUniverseLookup` serves),
    so the default security_type coverage matches the actual PIT membership
    contract rather than every ever-imported row.

    Each interval is ``(effective_start, effective_end, end_known_at)``.
    ``end_known_at`` is carried through (not dropped) so
    :func:`build_security_type_rows` can extend a default row's coverage
    through the same knowledge-lag window
    :class:`~data.universe.runtime.PITUniverseLookup` honors for a removal
    that is effective but not yet knowable (Codex P1, PR #42 second review) --
    the migration's ``ck_universe_membership_end_known_consistency`` CHECK
    constraint guarantees ``end_known_at`` is populated whenever
    ``effective_end`` is.
    """
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        latest_published = session.execute(
            select(UniverseImportBatch)
            .where(
                UniverseImportBatch.universe_id == universe_id,
                UniverseImportBatch.status == "published",
            )
            .order_by(UniverseImportBatch.published_at.desc())
        ).scalars().first()
        if latest_published is None:
            return {}
        rows = session.execute(
            select(UniverseMembership).where(
                UniverseMembership.universe_id == universe_id,
                UniverseMembership.import_batch_id == latest_published.id,
            )
        ).scalars().all()
    intervals: dict[str, list[tuple[date, Optional[date], Optional[datetime]]]] = {}
    for r in rows:
        end_known_at = r.end_known_at
        if end_known_at is not None and end_known_at.tzinfo is None:
            # SQLite loses tz awareness on read; stored values are UTC (same
            # normalization PITUniverseLookup applies to known_at/
            # end_known_at -- required here too so _removal_lag_boundary_date's
            # comparison against session_close_cutoff's tz-aware UTC
            # datetimes doesn't raise TypeError).
            end_known_at = end_known_at.replace(tzinfo=timezone.utc)
        intervals.setdefault(r.ticker, []).append(
            (r.effective_start, r.effective_end, end_known_at)
        )
    return intervals


def build_security_type_rows(
    membership_intervals: dict[str, list[tuple[date, Optional[date], Optional[datetime]]]],
    curation: list[SecurityTypeCurationEntry],
    *,
    default_security_type: str = DEFAULT_SECURITY_TYPE,
) -> list[dict]:
    """Build ``security_type`` attribute rows for every ticker with a
    membership interval.

    A ticker with one or more explicit curation entries uses ONLY those
    entries -- an operator who hand-curates a ticker takes responsibility
    for covering its whole relevant history; a gap left inside a curated
    ticker's membership span is a genuine, surfaced coverage gap (visible
    via :func:`eligibility_coverage_report`), never silently papered over
    with the default classification (that would risk overwriting a
    deliberately-narrower curated fact with a guess). A ticker with NO
    curation entries at all gets the ``default_security_type`` for its full
    known membership span(s) -- this is the "largely static, hand-curate the
    rare exceptions" design (§5.1).

    Codex P1 fix (PR #42 second review): a closed membership interval's
    ``effective_end`` is the RAW removal date, but
    :class:`~data.universe.runtime.PITUniverseLookup` still counts the
    ticker as eligible past that raw date until ``end_known_at``'s cutoff --
    a date-only source's removal is only knowable at the next trading
    session's close (the same knowledge-lag rule as membership entries,
    ``data/universe/calendar.py::conservative_known_at_for_date_only_source``).
    Copying the raw ``effective_end`` into the default security_type row
    made eligibility (missing_attribute) diverge from membership on exactly
    those knowledge-lag day(s): a strategy filtering on ``security_type``/
    ``allowed_security_types`` would drop a still-membership-eligible ticker
    before its removal was actually knowable -- using not-yet-knowable
    information to end coverage early is itself a look-ahead-shaped defect,
    the same class this repo already fixed for membership (BUG-008). The
    default row's ``effective_end`` is now :func:`_removal_lag_boundary_date`
    of ``end_known_at`` -- the exact half-open boundary at which
    :class:`~data.universe.runtime.PITUniverseLookup`'s per-date,
    per-cutoff check stops counting the ticker eligible, cutoff-hour-aware
    (Codex P2 fix, round 2: not simply ``end_known_at.date()``, which
    under-covers when a removal announcement lands after its own date's
    session-close cutoff) -- not the raw membership ``effective_end``.
    """
    curated_by_ticker: dict[str, list[SecurityTypeCurationEntry]] = {}
    for entry in curation:
        if entry.effective_end is not None and entry.effective_end <= entry.effective_start:
            raise SecurityTypeCurationError(
                f"curation entry for {entry.ticker!r} has effective_end "
                f"{entry.effective_end} <= effective_start {entry.effective_start}"
            )
        curated_by_ticker.setdefault(entry.ticker, []).append(entry)

    for ticker, entries in curated_by_ticker.items():
        ordered = sorted(entries, key=lambda e: e.effective_start)
        for prev, curr in zip(ordered, ordered[1:]):
            prev_end = prev.effective_end
            if prev_end is None or prev_end > curr.effective_start:
                raise SecurityTypeCurationError(
                    f"overlapping curation entries for ticker {ticker!r}: "
                    f"[{prev.effective_start},{prev.effective_end}) overlaps "
                    f"[{curr.effective_start},{curr.effective_end})"
                )

    rows: list[dict] = []
    for ticker, intervals in membership_intervals.items():
        if ticker in curated_by_ticker:
            for entry in curated_by_ticker[ticker]:
                rows.append(
                    dict(
                        ticker=ticker,
                        attribute_name=SECURITY_TYPE_ATTRIBUTE_NAME,
                        attribute_value_numeric=None,
                        attribute_value_text=entry.security_type,
                        effective_start=entry.effective_start,
                        effective_end=entry.effective_end,
                        computed_from=_security_type_computed_from(
                            _CURATED_SOURCE_TAG,
                            f"hand-curated ({entry.note})" if entry.note else "hand-curated",
                        ),
                        source_data_asof=entry.effective_start,
                    )
                )
        else:
            for start, end, end_known_at in intervals:
                if end is None:
                    default_effective_end = None  # open interval, unchanged
                elif end_known_at is not None:
                    # Extend through the knowledge-lag window using the
                    # exact cutoff-aware boundary (Codex P2 fix, PR #42
                    # second review, round 2) -- not merely
                    # end_known_at.date(), which is only correct when
                    # end_known_at falls at-or-before its own date's cutoff.
                    default_effective_end = _removal_lag_boundary_date(end_known_at)
                else:
                    # Defensive fallback only -- the schema CHECK constraint
                    # guarantees end_known_at is populated whenever
                    # effective_end is, so this branch should be unreachable
                    # against real data.
                    default_effective_end = end
                rows.append(
                    dict(
                        ticker=ticker,
                        attribute_name=SECURITY_TYPE_ATTRIBUTE_NAME,
                        attribute_value_numeric=None,
                        attribute_value_text=default_security_type,
                        effective_start=start,
                        effective_end=default_effective_end,
                        computed_from=_security_type_computed_from(
                            _DEFAULT_SOURCE_TAG,
                            f"default classification ({default_security_type})",
                        ),
                        source_data_asof=start,
                    )
                )

    # Any curated ticker with NO membership interval at all is a curation
    # error (curating a ticker that was never a member of this universe_id).
    unknown = set(curated_by_ticker) - set(membership_intervals)
    if unknown:
        raise SecurityTypeCurationError(
            f"curation entries reference ticker(s) {sorted(unknown)!r} with no "
            "membership interval in this universe_id -- check for a typo or a "
            "mismatched universe_id."
        )

    return rows


def write_security_type_batch(
    engine: Union[Engine, str],
    universe_id: str,
    curation: list[SecurityTypeCurationEntry],
    *,
    code_version: str,
    default_security_type: str = DEFAULT_SECURITY_TYPE,
    computed_at: Optional[datetime] = None,
    notes: Optional[str] = None,
) -> EligibilityBatchWriteResult:
    """Compute and persist one ``security_type`` computation batch (default
    classification + hand-curated overrides) against the universe's latest
    published membership."""
    if isinstance(engine, str):
        engine = create_engine(engine)
    Base.metadata.create_all(engine)

    membership_intervals = load_membership_intervals(engine, universe_id)
    if not membership_intervals:
        raise EmptyBatchError(
            f"No published universe_membership rows found for "
            f"universe_id={universe_id!r}; run "
            "scripts/import_universe_membership.py first."
        )
    rows = build_security_type_rows(
        membership_intervals, curation, default_security_type=default_security_type
    )
    if not rows:
        raise EmptyBatchError(
            f"build_security_type_rows produced zero rows for universe_id={universe_id!r}."
        )

    computed_at = computed_at or datetime.now(timezone.utc)
    with Session(engine) as session:
        batch = UniverseEligibilityBatch(
            universe_id=universe_id,
            code_version=code_version,
            computed_at=computed_at,
            n_attribute_rows=len(rows),
            notes=notes,
            created_at=computed_at,
        )
        session.add(batch)
        session.flush()
        for row in rows:
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=universe_id,
                    computation_batch_id=batch.id,
                    created_at=computed_at,
                    **row,
                )
            )
        session.commit()
        batch_id = batch.id

    logger.info(
        "security_type_batch_written",
        universe_id=universe_id,
        batch_id=batch_id,
        n_rows=len(rows),
        n_curated=len({r["ticker"] for r in rows if _is_curated_security_type_row(r["computed_from"])}),
    )
    return EligibilityBatchWriteResult(
        batch_id=batch_id,
        n_rows_written=len(rows),
        n_tickers=len({r["ticker"] for r in rows}),
        n_dates=len({r["effective_start"] for r in rows}),
        attribute_names=(SECURITY_TYPE_ATTRIBUTE_NAME,),
    )


# ─── §3: coverage report ───────────────────────────────────────────────────────


@dataclass
class EligibilityCoverageReport:
    by_date: pd.DataFrame
    excluded_attributes: dict
    n_security_type_curated_tickers: int
    n_security_type_default_tickers: int


def eligibility_coverage_report(
    engine: Union[Engine, str],
    universe_id: str,
    dates: list[date],
    attribute_names: tuple[str, ...] = (
        "adv_usd_20d",
        "price_usd",
        SECURITY_TYPE_ATTRIBUTE_NAME,
    ),
) -> EligibilityCoverageReport:
    """Coverage report by date/attribute: how many PIT members have a
    covering eligibility-attribute row versus a gap (design doc §5.2's 03A-4
    acceptance evidence, mirroring
    ``data/universe/import_pipeline.py::coverage_report``'s membership-axis
    precedent).

    For each requested date, membership is resolved the same knowledge-gated
    way callers actually query it
    (:class:`~data.universe.runtime.PITUniverseLookup`), so a per-attribute
    "missing" count reflects a real, actionable eligibility gap rather than a
    raw interval-boundary artifact. Dates outside the published membership
    coverage window report ``None`` counts (excluded from the gap tally)
    rather than raising, so the report stays usable for auditing
    requested-vs-certified ranges.
    """
    if isinstance(engine, str):
        engine = create_engine(engine)
    Base.metadata.create_all(engine)

    from data.universe.runtime import CoverageGapError, NoPublishedImportError, PITUniverseLookup

    try:
        membership_lookup: Optional[PITUniverseLookup] = PITUniverseLookup(engine, universe_id)
    except NoPublishedImportError:
        membership_lookup = None

    # Codex P3 fix (03A-4b PR #42 review): one query covering both the
    # caller's requested attribute_names AND security_type (needed below for
    # the curated-vs-default counts regardless of whether the caller
    # requested security_type in `attribute_names`), instead of two separate
    # round trips.
    query_attribute_names = set(attribute_names) | {SECURITY_TYPE_ATTRIBUTE_NAME}
    with Session(engine) as session:
        attr_rows = session.execute(
            select(UniverseEligibilityAttribute).where(
                UniverseEligibilityAttribute.universe_id == universe_id,
                UniverseEligibilityAttribute.attribute_name.in_(query_attribute_names),
            )
        ).scalars().all()

    # (ticker, attribute_name) -> list of (start, end), restricted to the
    # caller's requested attribute_names for the by-date coverage loop below
    # (security_type rows are excluded here unless the caller asked for it,
    # matching pre-existing behavior).
    by_ticker_attr: dict[tuple[str, str], list[tuple[date, Optional[date]]]] = {}
    for row in attr_rows:
        if row.attribute_name not in attribute_names:
            continue
        by_ticker_attr.setdefault((row.ticker, row.attribute_name), []).append(
            (row.effective_start, row.effective_end)
        )

    # Codex P2 fix (03A-4b PR #42 review): pre-merge each (ticker, attribute)
    # key's intervals into a sorted, non-overlapping list ONCE (append-only
    # batches can legitimately produce overlapping/duplicate coverage across
    # re-runs, so a naive per-index bisect would be unsafe -- merging first
    # makes it safe), then binary-search per query instead of an O(n) linear
    # scan repeated for every (date, ticker, attribute) triple. This matters
    # at the full-history scale this module is designed for (potentially one
    # row per ticker per trading session over years of history).
    merged_by_ticker_attr: dict[tuple[str, str], list[tuple[date, Optional[date]]]] = {
        key: _merge_intervals(intervals) for key, intervals in by_ticker_attr.items()
    }

    def _covered(ticker: str, attribute_name: str, d: date) -> bool:
        intervals = merged_by_ticker_attr.get((ticker, attribute_name))
        if not intervals:
            return False
        starts = [iv[0] for iv in intervals]
        idx = bisect.bisect_right(starts, d) - 1
        if idx < 0:
            return False
        start, end = intervals[idx]
        return start <= d and (end is None or d < end)

    report_rows = []
    for d in sorted(dates):
        members: set[str] = set()
        in_coverage = False
        if membership_lookup is not None:
            try:
                members = set(membership_lookup.load_universe_as_of(d).eligible_tickers)
                in_coverage = True
            except CoverageGapError:
                in_coverage = False

        for attribute_name in attribute_names:
            if not in_coverage:
                report_rows.append(
                    {
                        "date": d,
                        "attribute_name": attribute_name,
                        "in_coverage": False,
                        "n_members": None,
                        "n_with_attribute": None,
                        "n_missing": None,
                    }
                )
                continue
            n_with = sum(1 for t in members if _covered(t, attribute_name, d))
            report_rows.append(
                {
                    "date": d,
                    "attribute_name": attribute_name,
                    "in_coverage": True,
                    "n_members": len(members),
                    "n_with_attribute": n_with,
                    "n_missing": len(members) - n_with,
                }
            )

    security_type_rows = [r for r in attr_rows if r.attribute_name == SECURITY_TYPE_ATTRIBUTE_NAME]
    curated_tickers = {r.ticker for r in security_type_rows if _is_curated_security_type_row(r.computed_from)}
    default_tickers = {r.ticker for r in security_type_rows} - curated_tickers

    return EligibilityCoverageReport(
        by_date=pd.DataFrame(report_rows),
        excluded_attributes=dict(EXCLUDED_ATTRIBUTES),
        n_security_type_curated_tickers=len(curated_tickers),
        n_security_type_default_tickers=len(default_tickers),
    )


def _merge_intervals(
    intervals: list[tuple[date, Optional[date]]]
) -> list[tuple[date, Optional[date]]]:
    """Merge possibly-overlapping half-open ``[start, end)`` intervals
    (``end=None`` meaning open/unbounded) into a sorted, non-overlapping
    list, so a query only ever needs to inspect one candidate interval."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged: list[list[Optional[date]]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        last_start, last_end = last[0], last[1]
        if last_end is None:
            continue  # already open-ended; nothing later can extend it
        if start <= last_end:
            last[1] = None if end is None else max(last_end, end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]

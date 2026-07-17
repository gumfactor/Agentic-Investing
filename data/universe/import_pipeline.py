"""Provider-agnostic import pipeline for point-in-time universe membership.

Implements docs/plans/01b-research-validity-design.md §1.2 steps 1-6:

1. ``persist_raw_snapshot`` — save the raw source response with a checksum
   and source version.
2. ``build_staging_records`` — normalize symbols and membership intervals
   into staging records.
3. ``validate_staging`` — reject overlaps, inverted dates, unknown symbols,
   and coverage gaps.
4. ``derive_known_at`` — derive/validate ``known_at`` using the conservative
   date-only availability rule; reject records that cannot meet it.
5. ``publish`` — publish only a complete, validated import.
6. ``coverage_report`` — coverage report by date (constituent counts, price
   joins, exclusions, unresolved mappings).

``run_import`` orchestrates all six steps for a given
``ConstituentProvider``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import structlog
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from data.universe.calendar import conservative_known_at_for_date_only_source
from data.universe.models import Base, SymbolHistory, UniverseImportBatch, UniverseMembership
from data.universe.providers.base import ChangeEvent, ConstituentProvider, ParsedConstituentData, RawSnapshot

logger = structlog.get_logger(__name__)

_TICKER_RENAME_PATTERN = re.compile(r"ticker\s+symbol", re.IGNORECASE)
_VALID_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")

_CONTENT_TYPE_EXTENSIONS = {
    "text/html": "html",
    "application/json": "json",
}


class ImportValidationError(Exception):
    """Raised when a staging bundle fails validation. Carries all issues found."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


# ─── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class MembershipCandidate:
    ticker: str
    vendor_symbol: Optional[str]
    effective_start: date
    effective_end: Optional[date]
    source: str
    source_record_id: str
    reason: Optional[str]
    announced_at: Optional[datetime] = None
    known_at: Optional[datetime] = None
    # Provider-supplied announcement of the REMOVAL closing this interval.
    end_announced_at: Optional[datetime] = None
    end_known_at: Optional[datetime] = None
    left_censored: bool = False


@dataclass
class SymbolHistoryCandidate:
    old_ticker: str
    new_ticker: str
    effective_date: date
    source: str
    source_record_id: str
    reason: Optional[str]
    known_at: Optional[datetime] = None


@dataclass
class StagingBundle:
    universe_id: str
    coverage_start: date
    membership: list[MembershipCandidate] = field(default_factory=list)
    symbol_history: list[SymbolHistoryCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_ticker_rename_event(event: ChangeEvent) -> bool:
    """True if a change-event row represents a same-entity ticker rename.

    Heuristic per docs/plans/01b2-constituent-source-contract.md: both an
    added and a removed ticker on the same row, with a free-text reason that
    mentions a ticker/symbol change. A row that doesn't match this pattern is
    always treated as an ordinary removal+addition pair — no continuity is
    ever fabricated.
    """
    if not event.added_ticker or not event.removed_ticker:
        return False
    if not event.reason:
        return False
    return bool(_TICKER_RENAME_PATTERN.search(event.reason))


# ─── Step 1: raw persistence ──────────────────────────────────────────────────


def persist_raw_snapshot(raw: RawSnapshot, artifact_root: Path) -> tuple[Path, str]:
    """Save the raw source response with a checksum and source version.

    Layout (Codex PR #34 P2 — unique per retrieval so a re-run can never
    silently overwrite a prior import's raw evidence):
    ``<artifact_root>/<provider_name>/<retrieved_at date>/<HHMMSSZ>-<checksum12>/raw.<ext>``
    plus a sibling ``manifest.json`` recording checksum, retrieval time,
    source version, origin URL, and (for CC BY-SA sources) an attribution
    note. Re-persisting byte-identical content to the same path is
    idempotent; a pre-existing path with different bytes is refused
    (fail closed) — with the checksum in the path this indicates tampering
    or a hash collision, never a legitimate re-import.
    """
    ext = _CONTENT_TYPE_EXTENSIONS.get(raw.content_type, "bin")
    checksum = hashlib.sha256(raw.content).hexdigest()
    stamp = raw.retrieved_at.strftime("%H%M%SZ")
    day_dir = (
        artifact_root
        / raw.provider_name
        / raw.retrieved_at.date().isoformat()
        / f"{stamp}-{checksum[:12]}"
    )
    day_dir.mkdir(parents=True, exist_ok=True)

    raw_path = day_dir / f"raw.{ext}"
    if raw_path.exists():
        existing = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if existing != checksum:
            raise ValueError(
                f"Raw artifact path {raw_path} already exists with different content "
                f"(existing sha256 {existing}, new {checksum}). Refusing to overwrite "
                "prior import evidence."
            )
    raw_path.write_bytes(raw.content)
    manifest = {
        "provider_name": raw.provider_name,
        "source_version": raw.source_version,
        "retrieved_at": raw.retrieved_at.isoformat(),
        "content_type": raw.content_type,
        "origin_url": raw.origin_url,
        "checksum_sha256": checksum,
        "n_bytes": len(raw.content),
        "attribution": (
            "Content derived from Wikipedia, licensed CC BY-SA 4.0. "
            "See docs/plans/01b2-constituent-source-contract.md."
            if "wikipedia" in raw.provider_name
            else "Synthetic FIXTURE data — not from any real source."
        ),
    }
    (day_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    logger.info(
        "raw_snapshot_persisted",
        provider=raw.provider_name,
        path=str(raw_path),
        checksum=checksum,
    )
    return raw_path, checksum


# ─── Step 2: staging normalization ────────────────────────────────────────────


def build_staging_records(
    parsed: ParsedConstituentData,
    coverage_start: date,
    source: str,
    source_version: str,
) -> StagingBundle:
    """Normalize symbols and membership intervals into staging records.

    Reconstruction algorithm (see docs/plans/01b2-constituent-source-contract.md
    "Reconstruction algorithm"):

    1. Every current-constituents row becomes an authoritative open interval.
    2. Change events matching :func:`is_ticker_rename_event` become
       ``SymbolHistoryCandidate`` rows (never a membership gap for the
       underlying entity — see the contract doc).
    3. Every change event is also decomposed into up to two atomic
       add/remove sub-events per ticker, used to reconstruct closed-out
       intervals not already covered by the authoritative current-row
       interval. Sub-events at/after a ticker's authoritative current-row
       start are treated as redundant confirmation, not a new interval
       (real Wikipedia data repeats a still-active ticker's original
       addition in both tables).
    4. A ticker with no current-row interval whose earliest event inside the
       window is a "removed" (no matching earlier "added") is left-censored:
       its interval is assumed to start at ``coverage_start`` and flagged
       ``left_censored=True``.
    """
    bundle = StagingBundle(universe_id=parsed.universe_id, coverage_start=coverage_start)

    current_by_ticker = {row.ticker: row for row in parsed.current_rows}

    # Per-ticker atomic (kind, date, source_record_id, reason, announced_at) events.
    atomic: dict[str, list[tuple[str, date, str, Optional[str], Optional[datetime]]]] = {}

    def _add_atomic(
        ticker: str,
        kind: str,
        d: date,
        source_record_id: str,
        reason: Optional[str],
        announced_at: Optional[datetime],
    ) -> None:
        atomic.setdefault(ticker, []).append((kind, d, source_record_id, reason, announced_at))

    for event in parsed.change_events:
        if (
            event.added_ticker
            and event.removed_ticker
            and event.added_ticker == event.removed_ticker
        ):
            # Same symbol added and removed on the same effective date: the
            # constituent slot changed hands but the ticker's membership is
            # continuous (real example: 2011-12-12 "Nicor acquired by AGL,
            # which retained the GAS ticker"). Producing an add+remove pair
            # here would create an empty [d, d) interval, so treat it as
            # continuity and record a warning for the coverage report.
            bundle.warnings.append(
                f"{event.added_ticker}: same-symbol replacement on "
                f"{event.effective_date} treated as continuous membership "
                f"({event.reason or 'no reason given'})."
            )
            continue

        if is_ticker_rename_event(event):
            bundle.symbol_history.append(
                SymbolHistoryCandidate(
                    old_ticker=event.removed_ticker,  # type: ignore[arg-type]
                    new_ticker=event.added_ticker,  # type: ignore[arg-type]
                    effective_date=event.effective_date,
                    source=source,
                    source_record_id=event.source_record_id,
                    reason=event.reason,
                )
            )
            # A rename still closes the old ticker's interval and opens the
            # new ticker's interval, so daily_prices joins (keyed by the raw
            # vendor ticker active on each date) keep resolving correctly.
            _add_atomic(event.removed_ticker, "removed", event.effective_date, event.source_record_id, event.reason, event.announced_at)  # type: ignore[arg-type]
            _add_atomic(event.added_ticker, "added", event.effective_date, event.source_record_id, event.reason, event.announced_at)  # type: ignore[arg-type]
            continue

        if event.added_ticker:
            _add_atomic(event.added_ticker, "added", event.effective_date, event.source_record_id, event.reason, event.announced_at)
        if event.removed_ticker:
            _add_atomic(event.removed_ticker, "removed", event.effective_date, event.source_record_id, event.reason, event.announced_at)

    all_tickers = set(current_by_ticker) | set(atomic)
    for ticker in sorted(all_tickers):
        events = sorted(atomic.get(ticker, []), key=lambda e: e[1])
        current_row = current_by_ticker.get(ticker)

        if current_row is not None:
            bundle.membership.append(
                MembershipCandidate(
                    ticker=ticker,
                    vendor_symbol=None,
                    effective_start=current_row.effective_start,
                    effective_end=None,
                    source=source,
                    source_record_id=current_row.source_record_id,
                    reason=None,
                )
            )
            # Only events strictly before the authoritative interval start
            # represent an earlier, now-closed stint (re-entry case).
            events = [e for e in events if e[1] < current_row.effective_start]

        # Pair remaining events chronologically into closed intervals.
        pending_start: Optional[tuple[date, str, Optional[str], Optional[datetime]]] = None
        for kind, d, source_record_id, reason, announced_at in events:
            if kind == "added":
                if pending_start is not None:
                    # Two "added" in a row with no intervening "removed":
                    # keep the earlier one, warn, and drop the duplicate.
                    bundle.warnings.append(
                        f"{ticker}: duplicate 'added' event at {d} with no intervening "
                        f"'removed'; keeping the earlier open date {pending_start[0]}."
                    )
                    continue
                pending_start = (d, source_record_id, reason, announced_at)
            elif kind == "removed":
                if pending_start is None:
                    if d <= coverage_start:
                        # A removal on/before the coverage window's own start
                        # gives no evidence of membership inside the
                        # certified window at all (the interval would have
                        # zero or negative length) — drop it rather than
                        # fabricate a same-day phantom membership.
                        bundle.warnings.append(
                            f"{ticker}: 'removed' at {d} is on/before coverage_start "
                            f"{coverage_start}; no left-censored interval created "
                            "(no evidence of in-window membership)."
                        )
                        continue
                    # Left-censored: was already a member when the window opens.
                    bundle.membership.append(
                        MembershipCandidate(
                            ticker=ticker,
                            vendor_symbol=None,
                            effective_start=coverage_start,
                            effective_end=d,
                            source=source,
                            source_record_id=source_record_id,
                            reason="left_censored_pre_coverage_window",
                            end_announced_at=announced_at,
                            left_censored=True,
                        )
                    )
                else:
                    start_date, start_record_id, _start_reason, start_announced_at = pending_start
                    bundle.membership.append(
                        MembershipCandidate(
                            ticker=ticker,
                            vendor_symbol=None,
                            effective_start=start_date,
                            effective_end=d,
                            source=source,
                            source_record_id=f"{start_record_id}->{source_record_id}",
                            reason=reason,
                            announced_at=start_announced_at,
                            end_announced_at=announced_at,
                        )
                    )
                    pending_start = None

        if pending_start is not None and current_row is None:
            # An "added" with no subsequent "removed" and no current-row
            # confirmation: the source is internally inconsistent (a still-
            # active ticker should appear in the current-constituents table).
            # Keep it open but flag it rather than silently dropping data.
            start_date, start_record_id, _, start_announced_at = pending_start
            bundle.warnings.append(
                f"{ticker}: 'added' at {start_date} has no matching 'removed' event and "
                "is not present in the current-constituents table (inferred open interval)."
            )
            bundle.membership.append(
                MembershipCandidate(
                    ticker=ticker,
                    vendor_symbol=None,
                    effective_start=start_date,
                    effective_end=None,
                    source=source,
                    source_record_id=start_record_id,
                    reason="inferred_open_not_confirmed_by_current_table",
                    announced_at=start_announced_at,
                )
            )

    return bundle


# ─── Step 3: validation ───────────────────────────────────────────────────────


def validate_staging(
    bundle: StagingBundle,
    coverage_end: date,
    *,
    ingested_at: Optional[datetime] = None,
    min_members_for_coverage_check: int = 1,
) -> list[str]:
    """Reject overlaps, inverted dates, unknown symbols, and coverage gaps.

    Returns a list of human-readable issue strings; an empty list means the
    bundle is valid. Does not raise — callers decide whether to treat any
    issues as fatal (``run_import`` always does).
    """
    issues: list[str] = []
    ingested_at = ingested_at or datetime.now(tz=timezone.utc)

    def _check_symbol(sym: Optional[str], context: str) -> None:
        if sym is None or not _VALID_TICKER_PATTERN.match(sym):
            issues.append(f"unknown_symbol: {context} ticker {sym!r} does not look like a valid ticker.")

    intervals_by_ticker: dict[str, list[tuple[date, date]]] = {}

    for row in bundle.membership:
        _check_symbol(row.ticker, "membership")

        if row.effective_start is None:
            issues.append(f"missing_effective_start: {row.ticker} ({row.source_record_id})")
            continue

        if row.effective_end is not None and row.effective_end <= row.effective_start:
            issues.append(
                f"inverted_or_empty_range: {row.ticker} effective_start={row.effective_start} "
                f"effective_end={row.effective_end} ({row.source_record_id})"
            )
            continue

        if row.announced_at is not None and row.announced_at > ingested_at:
            issues.append(
                f"future_announced: {row.ticker} announced_at={row.announced_at} is after "
                f"ingested_at={ingested_at} ({row.source_record_id})"
            )

        if row.end_announced_at is not None and row.end_announced_at > ingested_at:
            issues.append(
                f"future_announced: {row.ticker} end_announced_at={row.end_announced_at} is "
                f"after ingested_at={ingested_at} ({row.source_record_id})"
            )

        end_for_sort = row.effective_end or date.max
        intervals_by_ticker.setdefault(row.ticker, []).append((row.effective_start, end_for_sort))

    for sh in bundle.symbol_history:
        _check_symbol(sh.old_ticker, "symbol_history.old_ticker")
        _check_symbol(sh.new_ticker, "symbol_history.new_ticker")
        if sh.old_ticker == sh.new_ticker:
            issues.append(f"symbol_history_noop_rename: {sh.old_ticker} ({sh.source_record_id})")

    # No overlapping intervals per ticker; adjacent (touching) intervals are allowed.
    for ticker, intervals in intervals_by_ticker.items():
        ordered = sorted(intervals)
        for (s1, e1), (s2, e2) in zip(ordered, ordered[1:]):
            if s2 < e1:
                issues.append(
                    f"overlapping_intervals: {ticker} [{s1},{e1}) overlaps [{s2},{e2})"
                )

    # Global coverage gap: at every interval boundary inside
    # [coverage_start, coverage_end], at least one ticker must be an active
    # member. A per-ticker absence is NOT a coverage failure — only a global
    # gap (zero total members) is.
    boundaries = {bundle.coverage_start, coverage_end}
    for intervals in intervals_by_ticker.values():
        for s, e in intervals:
            if bundle.coverage_start <= s <= coverage_end:
                boundaries.add(s)
            if e != date.max and bundle.coverage_start <= e <= coverage_end:
                boundaries.add(e)
    for boundary in sorted(boundaries):
        if boundary >= coverage_end:
            continue
        active = sum(
            1
            for intervals in intervals_by_ticker.values()
            for s, e in intervals
            if s <= boundary < e
        )
        if active < min_members_for_coverage_check:
            issues.append(f"global_coverage_gap: no active members as of {boundary}")

    return issues


# ─── Step 4: known_at derivation ──────────────────────────────────────────────


def derive_known_at(bundle: StagingBundle) -> StagingBundle:
    """Derive ``known_at`` using the conservative date-only availability rule.

    Every row in this pipeline comes from a date-only source (Wikipedia
    supplies no announcement timestamp — see the source contract). A record
    that lacks a parseable ``effective_start``/``effective_date`` cannot meet
    the conservative availability rule and is rejected by
    :func:`validate_staging` before this function is reached (missing dates
    are already flagged there).

    Left-censored rows are the exception to the next-session rule
    (adversarial-review fix, 01B-2): their ``effective_start`` is the
    fabricated ``coverage_start`` boundary, not a real membership *change*
    that had to become knowable — the security was already a member before
    the window opened. Applying the next-session rule to the boundary would
    wrongly exclude these members on day one of the certified window, so
    they get ``known_at`` = the session close of ``effective_start`` itself
    (still never earlier than any queryable cutoff for that date).
    """
    from data.universe.calendar import session_close_cutoff

    for row in bundle.membership:
        if row.left_censored:
            fallback = session_close_cutoff(row.effective_start)
        else:
            fallback = conservative_known_at_for_date_only_source(row.effective_start)
        row.known_at = max(row.announced_at, fallback) if row.announced_at else fallback
        # Removal availability (Codex PR #34 P2): a date-only removal
        # effective on session d is knowable only from the next session's
        # close. Runtime eligibility keeps the ticker in the universe until
        # then — the exit-side mirror of the entry known_at rule.
        if row.effective_end is not None:
            end_fallback = conservative_known_at_for_date_only_source(row.effective_end)
            # A provider-supplied removal announcement is preserved (Codex
            # PR #34 P2) and, like the entry side, can only make the removal
            # knowable LATER than the conservative date-only floor - a
            # removal announced after its effective date keeps the ticker
            # eligible until the announcement.
            row.end_known_at = (
                max(row.end_announced_at, end_fallback)
                if row.end_announced_at
                else end_fallback
            )
        else:
            row.end_known_at = None
    for sh in bundle.symbol_history:
        sh.known_at = conservative_known_at_for_date_only_source(sh.effective_date)
    return bundle


# ─── Step 5: publish ──────────────────────────────────────────────────────────


def publish(
    bundle: StagingBundle,
    *,
    engine: Engine,
    provider_name: str,
    source_version: str,
    raw_artifact_path: str,
    raw_checksum_sha256: str,
    retrieved_at: datetime,
    coverage_start: date,
    coverage_end: date,
    excluded_tickers_record: Optional[str] = None,
) -> UniverseImportBatch:
    """Publish a validated, known_at-derived staging bundle.

    ``excluded_tickers_record`` is the JSON audit string
    (``{"tickers": [...], "reason": "..."}``) for operator exclusions,
    persisted on the batch row so exclusions are DB-queryable.

    Only a complete, validated import is ever published (§1.2 step 5): call
    :func:`validate_staging` first and raise :class:`ImportValidationError`
    on any issue before calling this function. This function itself performs
    a defensive re-check and raises rather than silently publishing an
    invalid bundle.
    """
    issues = validate_staging(bundle, coverage_end)
    if issues:
        raise ImportValidationError(issues)
    if any(row.known_at is None for row in bundle.membership) or any(
        sh.known_at is None for sh in bundle.symbol_history
    ):
        raise ImportValidationError(["known_at_not_derived: call derive_known_at() before publish()"])
    if any(
        row.effective_end is not None and row.end_known_at is None
        for row in bundle.membership
    ):
        raise ImportValidationError(
            ["end_known_at_not_derived: call derive_known_at() before publish()"]
        )

    Base.metadata.create_all(engine)
    now = datetime.now(tz=timezone.utc)

    with Session(engine) as session:
        batch = UniverseImportBatch(
            universe_id=bundle.universe_id,
            provider=provider_name,
            source_version=source_version,
            raw_artifact_path=raw_artifact_path,
            raw_checksum_sha256=raw_checksum_sha256,
            retrieved_at=retrieved_at,
            status="published",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            n_membership_rows=len(bundle.membership),
            n_symbol_history_rows=len(bundle.symbol_history),
            excluded_tickers=excluded_tickers_record,
            created_at=now,
            published_at=now,
        )
        session.add(batch)
        session.flush()  # assign batch.id

        for row in bundle.membership:
            session.add(
                UniverseMembership(
                    universe_id=bundle.universe_id,
                    ticker=row.ticker,
                    vendor_symbol=row.vendor_symbol,
                    effective_start=row.effective_start,
                    effective_end=row.effective_end,
                    source=row.source,
                    source_record_id=row.source_record_id,
                    announced_at=row.announced_at,
                    known_at=row.known_at,
                    end_known_at=row.end_known_at,
                    source_version=source_version,
                    ingested_at=now,
                    reason=row.reason,
                    import_batch_id=batch.id,
                )
            )
        # Symbol history is a global append-only mapping (no batch scoping):
        # a coverage-advancing re-import re-derives the same rename rows, so
        # skip rows whose (universe_id, old_ticker, effective_date) key
        # already exists instead of tripping the unique constraint
        # (Codex PR #34 P1 fix — re-imports must succeed end to end).
        existing_keys = {
            (r.old_ticker, r.effective_date)
            for r in session.execute(
                select(SymbolHistory).where(
                    SymbolHistory.universe_id == bundle.universe_id
                )
            ).scalars()
        }
        for sh in bundle.symbol_history:
            if (sh.old_ticker, sh.effective_date) in existing_keys:
                continue
            session.add(
                SymbolHistory(
                    universe_id=bundle.universe_id,
                    old_ticker=sh.old_ticker,
                    new_ticker=sh.new_ticker,
                    effective_date=sh.effective_date,
                    source=sh.source,
                    source_record_id=sh.source_record_id,
                    known_at=sh.known_at,
                    source_version=source_version,
                    ingested_at=now,
                    reason=sh.reason,
                )
            )
        session.commit()
        session.refresh(batch)
        batch_id = batch.id

    logger.info(
        "universe_import_published",
        universe_id=bundle.universe_id,
        batch_id=batch_id,
        n_membership_rows=len(bundle.membership),
        n_symbol_history_rows=len(bundle.symbol_history),
        coverage_start=str(coverage_start),
        coverage_end=str(coverage_end),
    )
    with Session(engine) as session:
        return session.get(UniverseImportBatch, batch_id)


def record_rejected_batch(
    *,
    engine: Engine,
    universe_id: str,
    provider_name: str,
    source_version: str,
    raw_artifact_path: str,
    raw_checksum_sha256: str,
    retrieved_at: datetime,
    issues: list[str],
) -> UniverseImportBatch:
    """Write an auditable 'rejected' batch row for a failed validation."""
    Base.metadata.create_all(engine)
    now = datetime.now(tz=timezone.utc)
    with Session(engine) as session:
        batch = UniverseImportBatch(
            universe_id=universe_id,
            provider=provider_name,
            source_version=source_version,
            raw_artifact_path=raw_artifact_path,
            raw_checksum_sha256=raw_checksum_sha256,
            retrieved_at=retrieved_at,
            status="rejected",
            rejected_reason="; ".join(issues)[:4000],
            created_at=now,
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)
        batch_id = batch.id
    logger.warning("universe_import_rejected", universe_id=universe_id, issues=issues)
    with Session(engine) as session:
        return session.get(UniverseImportBatch, batch_id)


# ─── Step 6: coverage report ───────────────────────────────────────────────────


@dataclass
class CoverageReport:
    by_date: pd.DataFrame
    n_left_censored_intervals: int
    n_symbol_history_rows: int
    # Operator --exclude-tickers audit from the latest published batch
    # ({"tickers": [...], "reason": "..."}); None when nothing was excluded.
    excluded_tickers: Optional[dict] = None


def coverage_report(
    engine: Engine,
    universe_id: str,
    dates: list[date],
    prices: Optional[pd.DataFrame] = None,
) -> CoverageReport:
    """Coverage report by date: constituent counts, price joins, exclusions.

    Args:
        prices: optional long-format DataFrame with ``ticker``, ``date``
            columns, used to report how many members have/lack a same-date
            price row.
    """
    with Session(engine) as session:
        latest_published = session.execute(
            select(UniverseImportBatch)
            .where(
                UniverseImportBatch.universe_id == universe_id,
                UniverseImportBatch.status == "published",
            )
            .order_by(UniverseImportBatch.published_at.desc())
        ).scalars().first()
        excluded: Optional[dict] = None
        if latest_published is not None and latest_published.excluded_tickers:
            excluded = json.loads(latest_published.excluded_tickers)

        # Scope membership to the latest published batch — the same batch
        # PITUniverseLookup serves. Each batch is a complete row set, so an
        # unscoped read would double-count members after a re-import
        # (Codex PR #34 P1 fix).
        membership_query = select(UniverseMembership).where(
            UniverseMembership.universe_id == universe_id
        )
        if latest_published is not None:
            membership_query = membership_query.where(
                UniverseMembership.import_batch_id == latest_published.id
            )
        rows = session.execute(membership_query).scalars().all()
        n_left_censored = sum(1 for r in rows if r.reason == "left_censored_pre_coverage_window")
        n_symbol_history = session.execute(
            select(SymbolHistory).where(SymbolHistory.universe_id == universe_id)
        ).scalars().all()

    priced_tickers_by_date: dict[date, set[str]] = {}
    if prices is not None and not prices.empty:
        # Normalize to datetime.date before grouping (Codex PR #34 P2):
        # pd.read_sql and similar loaders yield datetime64/Timestamp values,
        # whose keys never compare equal to the datetime.date values in the
        # `dates` argument — every member would silently report as unpriced.
        price_dates = pd.to_datetime(prices["date"]).dt.date
        for d, group in prices.groupby(price_dates):
            priced_tickers_by_date[d] = set(group["ticker"])

    report_rows = []
    for d in dates:
        members = {
            r.ticker
            for r in rows
            if r.effective_start <= d < (r.effective_end or date.max)
        }
        n_members = len(members)
        if prices is not None:
            priced = priced_tickers_by_date.get(d, set())
            n_priced = len(members & priced)
            n_unpriced = n_members - n_priced
        else:
            n_priced = None
            n_unpriced = None
        report_rows.append(
            {
                "date": d,
                "n_members": n_members,
                "n_priced_members": n_priced,
                "n_unpriced_members": n_unpriced,
            }
        )

    return CoverageReport(
        by_date=pd.DataFrame(report_rows),
        n_left_censored_intervals=n_left_censored,
        n_symbol_history_rows=len(n_symbol_history),
        excluded_tickers=excluded,
    )


# ─── Orchestration ─────────────────────────────────────────────────────────────


def apply_exclusions(
    parsed: ParsedConstituentData, excluded: set[str]
) -> ParsedConstituentData:
    """Remove excluded tickers from parsed source data, preserving the
    non-excluded side of two-sided change events.

    Codex PR #34 P1 fix: the previous predicate dropped an entire change row
    whenever EITHER side was excluded, so a legitimate replacement partner
    (e.g. PETM replacing the excluded SUN on 2012-10-10 in the real
    Wikipedia data) lost its addition event and ended up missing or
    left-censored with a wrong start date. Only the excluded ticker's side
    of an event is now blanked; an event is dropped only when nothing
    non-excluded remains.
    """
    import dataclasses

    kept_events: list[ChangeEvent] = []
    for e in parsed.change_events:
        added_excluded = e.added_ticker in excluded if e.added_ticker else False
        removed_excluded = e.removed_ticker in excluded if e.removed_ticker else False
        if not added_excluded and not removed_excluded:
            kept_events.append(e)
            continue
        replacement = dataclasses.replace(
            e,
            added_ticker=None if added_excluded else e.added_ticker,
            added_security_name=None if added_excluded else e.added_security_name,
            removed_ticker=None if removed_excluded else e.removed_ticker,
            removed_security_name=None if removed_excluded else e.removed_security_name,
        )
        if replacement.added_ticker is None and replacement.removed_ticker is None:
            continue  # nothing non-excluded remains
        kept_events.append(replacement)

    return ParsedConstituentData(
        universe_id=parsed.universe_id,
        current_rows=[r for r in parsed.current_rows if r.ticker not in excluded],
        change_events=kept_events,
    )


def run_import(
    provider: ConstituentProvider,
    *,
    engine: Engine,
    artifact_root: Path,
    coverage_start: date,
    coverage_end: Optional[date] = None,
    exclude_tickers: Optional[set[str]] = None,
    exclude_reason: Optional[str] = None,
) -> UniverseImportBatch:
    """Run the full import pipeline: fetch -> persist -> stage -> validate -> publish.

    On validation failure, writes an auditable ``status='rejected'`` batch
    row (never partial membership rows) and re-raises
    :class:`ImportValidationError`.

    Args:
        exclude_tickers: operator escape hatch for source rows the validator
            correctly rejects (e.g. Wikipedia ticker-collision rows where two
            different companies used the same symbol decades apart — AN was
            both Amoco and AutoNation; SUN was both SunAmerica and Sunoco —
            which a symbol-keyed reconstruction cannot disambiguate).
            Excluded tickers get NO membership intervals at all: for
            historical queries they are simply never eligible, which is the
            fail-closed direction. Every exclusion must be recorded in
            docs/plans/01b2-constituent-source-contract.md.
    """
    raw = provider.fetch()
    raw_path, checksum = persist_raw_snapshot(raw, artifact_root)
    coverage_end = coverage_end or raw.retrieved_at.date()

    parsed = provider.parse(raw)
    excluded_tickers_record: Optional[str] = None
    if exclude_tickers:
        excluded = {t.upper() for t in exclude_tickers}
        excluded_tickers_record = json.dumps(
            {
                "tickers": sorted(excluded),
                "reason": exclude_reason
                or "operator exclusion (see docs/plans/01b2-constituent-source-contract.md)",
            },
            sort_keys=True,
        )
        parsed = apply_exclusions(parsed, excluded)
        logger.warning(
            "universe_import_tickers_excluded",
            excluded=sorted(excluded),
            note="operator-excluded source rows; these tickers get no membership intervals (fail closed)",
        )
    bundle = build_staging_records(
        parsed, coverage_start=coverage_start, source=provider.provider_name, source_version=raw.source_version
    )
    bundle = derive_known_at(bundle)

    issues = validate_staging(bundle, coverage_end, ingested_at=raw.retrieved_at)
    if issues:
        record_rejected_batch(
            engine=engine,
            universe_id=parsed.universe_id,
            provider_name=provider.provider_name,
            source_version=raw.source_version,
            raw_artifact_path=str(raw_path),
            raw_checksum_sha256=checksum,
            retrieved_at=raw.retrieved_at,
            issues=issues,
        )
        raise ImportValidationError(issues)

    return publish(
        bundle,
        engine=engine,
        provider_name=provider.provider_name,
        source_version=raw.source_version,
        raw_artifact_path=str(raw_path),
        raw_checksum_sha256=checksum,
        retrieved_at=raw.retrieved_at,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        excluded_tickers_record=excluded_tickers_record,
    )

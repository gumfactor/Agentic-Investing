"""Runtime point-in-time universe API (BUG-008, design plan §1.3).

Two strictly separated modes:

- **Historical mode** — :func:`load_universe_as_of` / :class:`PITUniverseLookup`.
  Backed by a *published* universe import (see
  ``data/universe/import_pipeline.py``). Fails closed when the requested date
  is outside validated source coverage or when membership was not known by
  the observation cutoff. Returns :class:`HistoricalUniverse` — the only type
  historical IC/backtest/backfill code accepts.

- **Operational current mode** — :func:`load_current_universe`. A thin,
  explicitly-labeled wrapper over ``config.universe_loader.load_universe``
  (live current-membership fetch) returning
  :class:`CurrentUniverseSnapshot`. Historical code MUST reject this type;
  use :func:`require_historical_universe` at the boundary. This is
  enforcement at the type level, not by convention: a
  ``CurrentUniverseSnapshot`` is not iterable and does not expose its
  tickers under the same attribute protocol as ``HistoricalUniverse``, so it
  cannot be silently duck-typed into a historical path.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional, Union

import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from data.universe.calendar import session_close_cutoff
from data.universe.models import SymbolHistory, UniverseImportBatch, UniverseMembership

logger = structlog.get_logger(__name__)


# ─── Errors ───────────────────────────────────────────────────────────────────


class UniverseError(Exception):
    """Base class for point-in-time universe failures."""


class CoverageGapError(UniverseError):
    """Requested date is outside the validated coverage of any published import."""


class NoPublishedImportError(CoverageGapError):
    """No published import exists for the requested universe_id."""


class InsufficientCrossSectionError(UniverseError):
    """Eligible cross-section is below the caller's configured minimum."""


class CurrentUniverseRejectedError(TypeError, UniverseError):
    """A current-universe object was passed to historical research code."""


# ─── Result types ─────────────────────────────────────────────────────────────


class ExclusionReason(str, enum.Enum):
    NOT_A_MEMBER = "not_a_member"
    NOT_KNOWN_BY_CUTOFF = "membership_not_known_by_cutoff"


@dataclass(frozen=True)
class UniverseExclusion:
    ticker: str
    reason: ExclusionReason
    detail: str


@dataclass(frozen=True)
class HistoricalUniverse:
    """Eligible tickers as of a date, under a knowledge cutoff. Historical-safe.

    The ONLY universe type historical IC/backtest/backfill code may accept.
    """

    universe_id: str
    as_of_date: date
    observation_cutoff: datetime
    eligible_tickers: tuple[str, ...]
    exclusions: tuple[UniverseExclusion, ...]
    import_batch_id: int
    source: str
    source_version: str
    coverage_start: date
    coverage_end: date

    def __contains__(self, ticker: str) -> bool:
        return ticker in set(self.eligible_tickers)


@dataclass(frozen=True)
class CurrentUniverseSnapshot:
    """Operational current-membership snapshot. NEVER valid for historical research.

    Deliberately does not share ``HistoricalUniverse``'s attribute protocol
    (no ``as_of_date``, no ``eligible_tickers``) so it cannot be duck-typed
    into a historical code path. Access tickers via ``.operational_tickers``.
    """

    operational_tickers: tuple[str, ...]
    fetched_at: datetime
    source: str


def require_historical_universe(obj: object) -> HistoricalUniverse:
    """Boundary guard for historical research code (§1.4: a current-universe
    loader cannot be passed to historical IC/backtest code)."""
    if isinstance(obj, HistoricalUniverse):
        return obj
    if isinstance(obj, CurrentUniverseSnapshot):
        raise CurrentUniverseRejectedError(
            "A CurrentUniverseSnapshot (operational current-membership mode) was "
            "passed to historical research code. Historical IC/backtest/backfill "
            "paths require a HistoricalUniverse from load_universe_as_of() / "
            "PITUniverseLookup (BUG-008)."
        )
    raise CurrentUniverseRejectedError(
        f"Historical research code requires a HistoricalUniverse; got "
        f"{type(obj).__name__}. Plain ticker lists are not accepted because their "
        "membership provenance cannot be verified (BUG-008)."
    )


# ─── PITUniverseLookup ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Interval:
    ticker: str
    effective_start: date
    effective_end: Optional[date]
    known_at: datetime
    # Availability of the removal event closing this interval; None if open.
    end_known_at: Optional[datetime] = None


class PITUniverseLookup:
    """In-memory as-of membership lookup over one published universe import.

    Loads all membership intervals for ``universe_id`` from the latest
    *published* import batch once, then answers per-date eligibility without
    further DB round trips (IC backfills query hundreds of dates).

    Fails closed at construction when no published import exists, and at
    query time when a date is outside the validated coverage window.
    """

    def __init__(self, engine: Union[Engine, str], universe_id: str) -> None:
        if isinstance(engine, str):
            engine = create_engine(engine)
        self._universe_id = universe_id

        with Session(engine) as session:
            batch = session.execute(
                select(UniverseImportBatch)
                .where(
                    UniverseImportBatch.universe_id == universe_id,
                    UniverseImportBatch.status == "published",
                )
                .order_by(UniverseImportBatch.published_at.desc())
            ).scalars().first()
            if batch is None:
                raise NoPublishedImportError(
                    f"No published universe import exists for universe_id={universe_id!r}. "
                    "Run scripts/import_universe_membership.py first; historical research "
                    "fails closed without validated membership (BUG-008)."
                )
            self._batch_id: int = batch.id
            self._source: str = batch.provider
            self._source_version: str = batch.source_version
            self._coverage_start: date = batch.coverage_start
            self._coverage_end: date = batch.coverage_end

            rows = session.execute(
                select(UniverseMembership).where(
                    UniverseMembership.universe_id == universe_id,
                    UniverseMembership.import_batch_id == self._batch_id,
                )
            ).scalars().all()
            self._intervals: dict[str, list[_Interval]] = {}
            for r in rows:
                known_at = r.known_at
                if known_at.tzinfo is None:
                    # SQLite loses tz awareness; stored values are UTC.
                    known_at = known_at.replace(tzinfo=timezone.utc)
                end_known_at = r.end_known_at
                if end_known_at is not None and end_known_at.tzinfo is None:
                    end_known_at = end_known_at.replace(tzinfo=timezone.utc)
                self._intervals.setdefault(r.ticker, []).append(
                    _Interval(
                        ticker=r.ticker,
                        effective_start=r.effective_start,
                        effective_end=r.effective_end,
                        known_at=known_at,
                        end_known_at=end_known_at,
                    )
                )

            self._symbol_history = session.execute(
                select(SymbolHistory).where(SymbolHistory.universe_id == universe_id)
            ).scalars().all()

    # ── Introspection ────────────────────────────────────────────────────────

    @property
    def universe_id(self) -> str:
        return self._universe_id

    @property
    def coverage_start(self) -> date:
        return self._coverage_start

    @property
    def coverage_end(self) -> date:
        return self._coverage_end

    @property
    def import_batch_id(self) -> int:
        return self._batch_id

    # ── Queries ──────────────────────────────────────────────────────────────

    def _check_coverage(self, as_of_date: date) -> None:
        if not (self._coverage_start <= as_of_date <= self._coverage_end):
            raise CoverageGapError(
                f"as_of_date {as_of_date} is outside the validated coverage window "
                f"[{self._coverage_start}, {self._coverage_end}] of published import "
                f"batch {self._batch_id} for universe_id={self._universe_id!r}. "
                "Historical research fails closed outside validated coverage (BUG-008). "
                "Re-run scripts/import_universe_membership.py to advance coverage."
            )

    def is_eligible(
        self,
        ticker: str,
        as_of_date: date,
        observation_cutoff: Optional[datetime] = None,
    ) -> bool:
        """True if ``ticker`` was a member on ``as_of_date`` AND that
        membership was knowable by ``observation_cutoff``.

        Per-ticker absence is valid non-membership, never an error; only an
        out-of-coverage date raises.

        Removal gating (Codex PR #34 P2): an interval whose ``effective_end``
        has passed still confers eligibility while the removal itself was
        not yet knowable by ``observation_cutoff`` — with a date-only source
        a removal effective on session ``d`` becomes knowable only at the
        next session's close, and excluding the ticker earlier would leak
        future removal information into the backtest (the exit-side
        mirror-image of the entry ``known_at`` rule).
        """
        self._check_coverage(as_of_date)
        cutoff = observation_cutoff or session_close_cutoff(as_of_date)
        for iv in self._intervals.get(ticker, ()):
            if _interval_confers_eligibility(iv, as_of_date, cutoff):
                return True
        return False

    def load_universe_as_of(
        self,
        as_of_date: date,
        observation_cutoff: Optional[datetime] = None,
        min_eligible: Optional[int] = None,
    ) -> HistoricalUniverse:
        """Eligible tickers plus structured exclusion reasons as of a date.

        Args:
            observation_cutoff: knowledge cutoff; defaults to the session
                close of ``as_of_date``. Membership changes with
                ``known_at`` after this cutoff are excluded with a
                structured reason.
            min_eligible: when set, raise
                :class:`InsufficientCrossSectionError` if fewer tickers are
                eligible — the fail-closed alternative to silently emitting
                research from a shrunken universe.
        """
        self._check_coverage(as_of_date)
        cutoff = observation_cutoff or session_close_cutoff(as_of_date)

        eligible: list[str] = []
        exclusions: list[UniverseExclusion] = []
        for ticker, intervals in self._intervals.items():
            # Interval covers the date under the knowledge cutoff: either the
            # date is inside [start, end), or the removal that would close it
            # was not yet knowable by the cutoff (Codex PR #34 P2 — the
            # exit-side mirror of the entry known_at rule).
            member_now = [
                iv
                for iv in intervals
                if iv.effective_start <= as_of_date
                and (
                    iv.effective_end is None
                    or as_of_date < iv.effective_end
                    or (iv.end_known_at is not None and iv.end_known_at > cutoff)
                )
            ]
            if not member_now:
                continue  # plain non-membership: not an exclusion event
            if any(iv.known_at <= cutoff for iv in member_now):
                eligible.append(ticker)
            else:
                earliest_known = min(iv.known_at for iv in member_now)
                exclusions.append(
                    UniverseExclusion(
                        ticker=ticker,
                        reason=ExclusionReason.NOT_KNOWN_BY_CUTOFF,
                        detail=(
                            f"membership interval covers {as_of_date} but was not "
                            f"knowable until {earliest_known.isoformat()} "
                            f"(cutoff {cutoff.isoformat()})"
                        ),
                    )
                )

        eligible.sort()
        if min_eligible is not None and len(eligible) < min_eligible:
            raise InsufficientCrossSectionError(
                f"Only {len(eligible)} tickers eligible for universe "
                f"{self._universe_id!r} as of {as_of_date} (cutoff {cutoff.isoformat()}); "
                f"caller requires at least {min_eligible}. Failing closed instead of "
                "emitting research from a silently shrunken universe (BUG-008)."
            )

        logger.debug(
            "universe_loaded_as_of",
            universe_id=self._universe_id,
            as_of_date=str(as_of_date),
            n_eligible=len(eligible),
            n_excluded=len(exclusions),
        )
        return HistoricalUniverse(
            universe_id=self._universe_id,
            as_of_date=as_of_date,
            observation_cutoff=cutoff,
            eligible_tickers=tuple(eligible),
            exclusions=tuple(exclusions),
            import_batch_id=self._batch_id,
            source=self._source,
            source_version=self._source_version,
            coverage_start=self._coverage_start,
            coverage_end=self._coverage_end,
        )


def _interval_confers_eligibility(iv: _Interval, as_of_date: date, cutoff: datetime) -> bool:
    """True if the interval makes its ticker eligible at (as_of_date, cutoff).

    Entry side: the membership must have been knowable (``known_at <=
    cutoff``). Exit side: the interval must cover the date, OR the removal
    closing it must not yet have been knowable (``end_known_at > cutoff``).
    """
    if iv.effective_start > as_of_date:
        return False
    if iv.known_at > cutoff:
        return False
    if iv.effective_end is None or as_of_date < iv.effective_end:
        return True
    return iv.end_known_at is not None and iv.end_known_at > cutoff


# ─── Module-level convenience (design plan §1.3 signature) ────────────────────


def load_universe_as_of(
    universe_id: str,
    as_of_date: date,
    observation_cutoff: Optional[datetime] = None,
    *,
    engine: Union[Engine, str],
    min_eligible: Optional[int] = None,
) -> HistoricalUniverse:
    """Single-call form of :meth:`PITUniverseLookup.load_universe_as_of`.

    For repeated queries across many dates, construct one
    :class:`PITUniverseLookup` and reuse it.
    """
    lookup = PITUniverseLookup(engine, universe_id)
    return lookup.load_universe_as_of(
        as_of_date, observation_cutoff=observation_cutoff, min_eligible=min_eligible
    )


# ─── Operational current mode ─────────────────────────────────────────────────


def load_current_universe() -> CurrentUniverseSnapshot:
    """OPERATIONAL current-membership universe — never for historical research.

    Explicit non-historical mode kept only for operational ingestion (daily
    price fetch for the paper pipeline). Wraps
    ``config.universe_loader.load_universe`` and preserves its fail-closed
    behavior (raises on fetch failure rather than returning an empty
    universe).
    """
    from config.universe_loader import load_universe as _operational_load

    tickers = _operational_load()
    return CurrentUniverseSnapshot(
        operational_tickers=tuple(tickers),
        fetched_at=datetime.now(tz=timezone.utc),
        source="config.universe_loader (current membership)",
    )

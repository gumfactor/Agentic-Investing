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
from data.universe.models import (
    SymbolHistory,
    UniverseEligibilityAttribute,
    UniverseEligibilityBatch,
    UniverseImportBatch,
    UniverseMembership,
)

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

    @staticmethod
    def _normalize_as_of(as_of_date: object) -> date:
        """Accept plain dates plus the pandas Timestamp/datetime values that
        read_sql/CSV/parquet loaders commonly produce (Codex PR #34 P2 —
        every PIT boundary normalizes its date dtype)."""
        if isinstance(as_of_date, datetime):
            return as_of_date.date()
        if isinstance(as_of_date, date):
            return as_of_date
        try:
            import pandas as pd

            return pd.Timestamp(as_of_date).date()
        except Exception as exc:
            raise TypeError(
                f"as_of_date must be a date-like value; got {type(as_of_date).__name__}"
            ) from exc

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
        out-of-coverage date raises. Timestamp/datetime inputs are
        normalized to plain dates.

        Removal gating (Codex PR #34 P2): an interval whose ``effective_end``
        has passed still confers eligibility while the removal itself was
        not yet knowable by ``observation_cutoff`` — with a date-only source
        a removal effective on session ``d`` becomes knowable only at the
        next session's close, and excluding the ticker earlier would leak
        future removal information into the backtest (the exit-side
        mirror-image of the entry ``known_at`` rule).
        """
        as_of_date = self._normalize_as_of(as_of_date)
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
        as_of_date = self._normalize_as_of(as_of_date)
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


# ─── PIT eligibility attributes (03A-4a, design plan §1.3) ────────────────────
#
# A second, independent PIT axis alongside membership above: a ticker can be
# a member on a date and still fail a strategy-declared eligibility filter
# (ADV, price, security type) on that same date. This section adds the
# runtime READ API only; the daily batch job that populates
# universe_eligibility_attributes (adv_usd_20d, price_usd) and the
# security_type curation backfill are Phase B (03A-4b).


class EligibilityFilterOp(str, enum.Enum):
    GTE = "gte"
    LTE = "lte"
    EQ = "eq"
    IN = "in"


class EligibilityExclusionReason(str, enum.Enum):
    MISSING_ATTRIBUTE = "missing_attribute"
    STALE_ATTRIBUTE = "stale_attribute"
    BELOW_THRESHOLD = "below_threshold"
    WRONG_TYPE = "wrong_type"


class EligibilityError(UniverseError):
    """Base class for point-in-time eligibility-attribute failures."""


class NoEligibilityDataError(EligibilityError):
    """No eligibility-attribute rows exist at all for the requested universe_id.

    Distinct from a per-ticker ``missing_attribute`` exclusion (a normal,
    expected outcome for individual tickers): this is a whole-universe
    configuration problem -- Phase B's batch job has never run for this
    universe_id -- and callers should treat it as a hard failure, not a
    per-ticker exclusion reason, so it cannot be silently absorbed into an
    "everyone excluded" result that looks like a legitimate illiquid
    universe.
    """


@dataclass(frozen=True)
class FilterSpec:
    """One strategy-declared eligibility filter, parsed from the strategy
    YAML's ``universe.eligibility`` block (§1.3). ``threshold`` is a float
    for ``GTE``/``LTE``/``EQ`` against a numeric attribute, or a tuple of
    strings for ``IN`` against a text attribute (e.g. ``security_type``).

    ``max_staleness_days``, when set, additionally excludes a ticker whose
    matching attribute row's ``source_data_asof`` is older than
    ``as_of_date - max_staleness_days`` with ``stale_attribute`` -- even
    though the row nominally covers the date -- catching the case where a
    batch stopped being recomputed but its last row's open interval still
    technically covers every later date.
    """

    attribute_name: str
    op: EligibilityFilterOp
    threshold: Union[float, tuple[str, ...]]
    max_staleness_days: Optional[int] = None


@dataclass(frozen=True)
class EligibilityExclusion:
    ticker: str
    attribute_name: str
    reason: EligibilityExclusionReason
    detail: str


@dataclass(frozen=True)
class EligibilityResult:
    """Per-filter pass/fail outcome for every ticker considered, as of a date.

    ``passing_tickers`` satisfied every filter in ``filters`` (an empty
    ``filters`` dict makes every considered ticker pass vacuously).
    ``exclusions`` carries one entry per (ticker, failed filter) so a
    reviewer can distinguish "member but illiquid" from "member but no
    eligibility data" (§1.5's combined-exclusion acceptance test) -- a
    ticker can appear multiple times if it fails more than one filter.
    """

    universe_id: str
    as_of_date: date
    filters: dict[str, FilterSpec]
    passing_tickers: tuple[str, ...]
    exclusions: tuple[EligibilityExclusion, ...]

    def __contains__(self, ticker: str) -> bool:
        return ticker in set(self.passing_tickers)


class PITEligibilityLookup:
    """In-memory as-of eligibility-attribute lookup for one universe_id.

    Loads every ``universe_eligibility_attributes`` row for ``universe_id``
    (across all computation batches) once, then answers per-date filter
    evaluation without further DB round trips. When more than one batch has
    a row covering the same ticker/attribute/date, the row from the batch
    with the latest ``computed_at`` wins (§1.2: "correcting a bad
    computation publishes a new batch rather than mutating rows in place";
    the newest batch's row is authoritative for any date it covers).

    Fails closed at construction when no eligibility-attribute rows exist at
    all for ``universe_id`` (:class:`NoEligibilityDataError`) -- distinct
    from the normal, expected per-ticker ``missing_attribute`` exclusion.
    """

    def __init__(self, engine: Union[Engine, str], universe_id: str) -> None:
        if isinstance(engine, str):
            engine = create_engine(engine)
        self._universe_id = universe_id

        with Session(engine) as session:
            rows = session.execute(
                select(UniverseEligibilityAttribute, UniverseEligibilityBatch.computed_at)
                .join(
                    UniverseEligibilityBatch,
                    UniverseEligibilityAttribute.computation_batch_id
                    == UniverseEligibilityBatch.id,
                )
                .where(UniverseEligibilityAttribute.universe_id == universe_id)
            ).all()
            if not rows:
                raise NoEligibilityDataError(
                    f"No universe_eligibility_attributes rows exist for "
                    f"universe_id={universe_id!r}. The Phase B eligibility batch "
                    "job has not populated this universe yet; historical "
                    "eligibility evaluation fails closed rather than treating "
                    "every ticker as passing (03A-4a)."
                )

            # (ticker, attribute_name) -> list of (row, batch_computed_at)
            self._rows: dict[tuple[str, str], list[tuple[UniverseEligibilityAttribute, datetime]]] = {}
            for row, computed_at in rows:
                if computed_at.tzinfo is None:
                    computed_at = computed_at.replace(tzinfo=timezone.utc)
                self._rows.setdefault((row.ticker, row.attribute_name), []).append(
                    (row, computed_at)
                )
            self._all_tickers = sorted({r.ticker for r, _ in rows})

    @property
    def universe_id(self) -> str:
        return self._universe_id

    def _resolve_attribute(
        self, ticker: str, attribute_name: str, as_of_date: date
    ) -> Optional[tuple[UniverseEligibilityAttribute, datetime]]:
        """Return the (row, batch_computed_at) covering ``as_of_date`` from
        the latest-computed batch that has a covering row, or ``None``."""
        candidates = [
            (row, computed_at)
            for row, computed_at in self._rows.get((ticker, attribute_name), ())
            if row.effective_start <= as_of_date
            and (row.effective_end is None or as_of_date < row.effective_end)
        ]
        if not candidates:
            return None
        # "Latest batch wins" (§1.2). Break computed_at ties deterministically
        # on computation_batch_id (higher id = later-created batch): two
        # batches can share an identical computed_at (second-precision
        # wall-clock, or a corrective script reusing a fixed timestamp), and
        # max()'s first-maximal-element behavior would otherwise silently
        # degrade the invariant to "DB insert order" and let a stale/bad row
        # outrank its correction.
        return max(candidates, key=lambda pair: (pair[1], pair[0].computation_batch_id))

    def evaluate(
        self,
        as_of_date: date,
        filters: dict[str, FilterSpec],
        tickers: Optional[list[str]] = None,
    ) -> EligibilityResult:
        """Evaluate every filter in ``filters`` for ``tickers`` (defaults to
        every ticker with any eligibility row) as of ``as_of_date``."""
        candidate_tickers = tickers if tickers is not None else self._all_tickers
        exclusions: list[EligibilityExclusion] = []
        passing: list[str] = []

        for ticker in candidate_tickers:
            ticker_ok = True
            for filter_name, spec in filters.items():
                resolved = self._resolve_attribute(ticker, spec.attribute_name, as_of_date)
                if resolved is None:
                    exclusions.append(
                        EligibilityExclusion(
                            ticker=ticker,
                            attribute_name=spec.attribute_name,
                            reason=EligibilityExclusionReason.MISSING_ATTRIBUTE,
                            detail=(
                                f"no {spec.attribute_name} eligibility row covers "
                                f"{as_of_date} for {ticker}"
                            ),
                        )
                    )
                    ticker_ok = False
                    continue
                row, _ = resolved

                if (
                    spec.max_staleness_days is not None
                    and (as_of_date - row.source_data_asof).days > spec.max_staleness_days
                ):
                    exclusions.append(
                        EligibilityExclusion(
                            ticker=ticker,
                            attribute_name=spec.attribute_name,
                            reason=EligibilityExclusionReason.STALE_ATTRIBUTE,
                            detail=(
                                f"{spec.attribute_name} source_data_asof "
                                f"{row.source_data_asof} is more than "
                                f"{spec.max_staleness_days} days before {as_of_date}"
                            ),
                        )
                    )
                    ticker_ok = False
                    continue

                ok, reason, detail = _evaluate_filter_value(row, spec)
                if not ok:
                    exclusions.append(
                        EligibilityExclusion(
                            ticker=ticker,
                            attribute_name=spec.attribute_name,
                            reason=reason,  # type: ignore[arg-type]
                            detail=detail,
                        )
                    )
                    ticker_ok = False

            if ticker_ok:
                passing.append(ticker)

        passing.sort()
        logger.debug(
            "eligibility_evaluated_as_of",
            universe_id=self._universe_id,
            as_of_date=str(as_of_date),
            n_filters=len(filters),
            n_passing=len(passing),
            n_exclusions=len(exclusions),
        )
        return EligibilityResult(
            universe_id=self._universe_id,
            as_of_date=as_of_date,
            filters=dict(filters),
            passing_tickers=tuple(passing),
            exclusions=tuple(exclusions),
        )


def _evaluate_filter_value(
    row: UniverseEligibilityAttribute, spec: FilterSpec
) -> tuple[bool, Optional[EligibilityExclusionReason], str]:
    """Compare a resolved attribute row's value against ``spec``.

    Returns ``(passes, reason_if_failed, detail)``. A type mismatch between
    the filter's expected value shape (numeric op vs. text ``IN`` op) and
    what the row actually stores is a ``wrong_type`` exclusion, not a
    crash -- defensive against a future attribute whose declared type does
    not match what a caller's ``FilterSpec`` assumed.
    """
    if spec.op is EligibilityFilterOp.IN:
        if row.attribute_value_text is None:
            return (
                False,
                EligibilityExclusionReason.WRONG_TYPE,
                f"{spec.attribute_name} row has no text value for an IN filter "
                f"(attribute_value_text is NULL)",
            )
        if not isinstance(spec.threshold, tuple):
            return (
                False,
                EligibilityExclusionReason.WRONG_TYPE,
                f"{spec.attribute_name} IN filter threshold must be a tuple of "
                f"strings, got {type(spec.threshold).__name__}",
            )
        if row.attribute_value_text in spec.threshold:
            return True, None, ""
        return (
            False,
            EligibilityExclusionReason.BELOW_THRESHOLD,
            f"{spec.attribute_name}={row.attribute_value_text!r} not in "
            f"{spec.threshold!r}",
        )

    if row.attribute_value_numeric is None:
        return (
            False,
            EligibilityExclusionReason.WRONG_TYPE,
            f"{spec.attribute_name} row has no numeric value for a "
            f"{spec.op.value} filter (attribute_value_numeric is NULL)",
        )
    if not isinstance(spec.threshold, (int, float)):
        return (
            False,
            EligibilityExclusionReason.WRONG_TYPE,
            f"{spec.attribute_name} {spec.op.value} filter threshold must be "
            f"numeric, got {type(spec.threshold).__name__}",
        )
    value = float(row.attribute_value_numeric)
    threshold = float(spec.threshold)
    if spec.op is EligibilityFilterOp.GTE:
        passed = value >= threshold
    elif spec.op is EligibilityFilterOp.LTE:
        passed = value <= threshold
    elif spec.op is EligibilityFilterOp.EQ:
        passed = value == threshold
    else:  # pragma: no cover - exhaustive over EligibilityFilterOp
        raise AssertionError(f"unhandled EligibilityFilterOp {spec.op!r}")
    if passed:
        return True, None, ""
    return (
        False,
        EligibilityExclusionReason.BELOW_THRESHOLD,
        f"{spec.attribute_name}={value} does not satisfy {spec.op.value} {threshold}",
    )


def load_eligibility_as_of(
    universe_id: str,
    as_of_date: date,
    filters: dict[str, FilterSpec],
    *,
    engine: Union[Engine, str],
    tickers: Optional[list[str]] = None,
) -> EligibilityResult:
    """Single-call form of :meth:`PITEligibilityLookup.evaluate` (§1.3).

    For repeated queries across many dates, construct one
    :class:`PITEligibilityLookup` and reuse it.
    """
    lookup = PITEligibilityLookup(engine, universe_id)
    return lookup.evaluate(as_of_date, filters, tickers=tickers)


@dataclass(frozen=True)
class CombinedEligibleUniverse:
    """Membership AND eligibility, evaluated together (§1.3): "no caller can
    apply one check without the other."""

    membership: HistoricalUniverse
    eligibility: EligibilityResult

    @property
    def eligible_tickers(self) -> tuple[str, ...]:
        """Tickers that are BOTH PIT members AND pass every eligibility
        filter -- the single set any scoring/backtest caller should trade."""
        return tuple(t for t in self.membership.eligible_tickers if t in self.eligibility)


def load_historical_universe_as_of(
    universe_id: str,
    as_of_date: date,
    filters: dict[str, FilterSpec],
    observation_cutoff: Optional[datetime] = None,
    *,
    engine: Union[Engine, str],
    min_eligible: Optional[int] = None,
) -> CombinedEligibleUniverse:
    """Combined membership + eligibility call site (§1.3).

    The one call every score-generation, IC-validation, and backtesting site
    should use going forward, so no caller can apply the membership check
    without the eligibility check or vice versa. ``filters`` may be empty
    (e.g. a strategy that declares no eligibility filters) -- eligibility
    then passes vacuously for every PIT member, and membership alone governs
    the result, exactly like calling :func:`load_universe_as_of` directly.

    Membership is evaluated first (fewer, cheaper rows); eligibility is then
    evaluated only over the membership-eligible tickers so a huge
    non-member universe never needs an eligibility lookup at all.
    """
    membership = load_universe_as_of(
        universe_id,
        as_of_date,
        observation_cutoff=observation_cutoff,
        engine=engine,
        min_eligible=None,  # min_eligible is enforced below, post-eligibility
    )

    if filters:
        eligibility = load_eligibility_as_of(
            universe_id,
            as_of_date,
            filters,
            engine=engine,
            tickers=list(membership.eligible_tickers),
        )
    else:
        eligibility = EligibilityResult(
            universe_id=universe_id,
            as_of_date=membership.as_of_date,
            filters={},
            passing_tickers=membership.eligible_tickers,
            exclusions=(),
        )

    combined = CombinedEligibleUniverse(membership=membership, eligibility=eligibility)

    if min_eligible is not None and len(combined.eligible_tickers) < min_eligible:
        raise InsufficientCrossSectionError(
            f"Only {len(combined.eligible_tickers)} tickers eligible (membership "
            f"AND eligibility filters) for universe {universe_id!r} as of "
            f"{as_of_date}; caller requires at least {min_eligible}. Failing "
            "closed instead of emitting research from a silently shrunken "
            "universe (03A-4a, mirrors BUG-008's min_eligible contract)."
        )

    return combined

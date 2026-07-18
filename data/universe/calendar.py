"""Minimal NYSE-approximate trading-session calendar.

Used by the universe-membership import pipeline (date-only source ->
conservative ``known_at`` derivation, §1.2 step 4) and by the runtime API
(``observation_cutoff`` defaults, §1.3). No live-DB or third-party calendar
dependency is available in this repo (see 01B-2 exploration notes); this is a
deliberately small, self-contained approximation:

- Weekends are never trading sessions.
- A fixed, closed-form US market holiday set is excluded (New Year's Day,
  MLK Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth,
  Independence Day, Labor Day, Thanksgiving, Christmas), observed-date rules
  included (a holiday falling on Saturday is observed the preceding Friday;
  on Sunday, the following Monday).

This calendar is intentionally conservative for the purpose it is used for
here: the point-in-time membership contract only needs "is this date a
session, and what is the next one" to derive a *no-earlier-than* availability
timestamp for date-only source records. An imperfect holiday list only ever
makes ``known_at`` later (more conservative) or earlier by at most one
session around a handful of holidays per year; it does not change the
half-open interval / no-overlap / coverage-window invariants this module
exists to protect. If a stricter calendar (e.g. ``pandas_market_calendars``)
is added as a dependency later, only this module needs to change.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from dateutil.easter import easter

# NYSE observes the close at 16:00 America/New_York. Rather than take a
# zoneinfo dependency for a single conversion, this module works in a fixed
# UTC offset that is conservative year-round: 16:00 ET is 20:00 UTC during
# EDT and 21:00 UTC during EST. Using 21:00 UTC as the cutoff for every
# session is the *later* (more conservative) of the two, which is the
# correct direction for a "no earlier than" availability rule.
_SESSION_CLOSE_CUTOFF_UTC_HOUR = 21


@lru_cache(maxsize=64)
def _holidays_for_year(year: int) -> frozenset[date]:
    """US market holidays for a given year, with observed-date shifting."""

    def _observed(d: date) -> date:
        if d.weekday() == 5:  # Saturday -> observed Friday
            return d - timedelta(days=1)
        if d.weekday() == 6:  # Sunday -> observed Monday
            return d + timedelta(days=1)
        return d

    def _nth_weekday(month: int, weekday: int, n: int) -> date:
        d = date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        d += timedelta(days=offset + 7 * (n - 1))
        return d

    def _last_weekday(month: int, weekday: int) -> date:
        if month == 12:
            d = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            d = date(year, month + 1, 1) - timedelta(days=1)
        offset = (d.weekday() - weekday) % 7
        return d - timedelta(days=offset)

    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(1, 0, 3),  # MLK Day - 3rd Monday of January
        _nth_weekday(2, 0, 3),  # Presidents' Day - 3rd Monday of February
        easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(5, 0),  # Memorial Day - last Monday of May
        _observed(date(year, 6, 19)),  # Juneteenth (observed from 2021)
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(9, 0, 1),  # Labor Day - 1st Monday of September
        _nth_weekday(11, 3, 4),  # Thanksgiving - 4th Thursday of November
        _observed(date(year, 12, 25)),  # Christmas
    }
    if year < 2021:
        holidays.discard(_observed(date(year, 6, 19)))

    # NEXT year's New Year's Day observed in THIS year (Codex PR #34 P2):
    # when Jan 1 of year+1 falls on a Saturday, the observed closure is the
    # preceding Friday, Dec 31 of THIS year — _holidays_for_year(year+1)
    # alone would lose it and make date-only membership changes around the
    # year boundary knowable one session too early. (NYSE has occasionally
    # stayed open on such Fridays — e.g. 2021-12-31 — but including the
    # closure is the conservative direction for this module's purpose:
    # known_at can only move later, never earlier.)
    next_new_year_observed = _observed(date(year + 1, 1, 1))
    if next_new_year_observed.year == year:
        holidays.add(next_new_year_observed)

    return frozenset(holidays)


def is_trading_session(d: date) -> bool:
    """True if ``d`` is a (approximate) NYSE trading session."""
    if d.weekday() >= 5:
        return False
    return d not in _holidays_for_year(d.year)


def next_trading_session(d: date) -> date:
    """First trading session strictly after ``d``."""
    candidate = d + timedelta(days=1)
    while not is_trading_session(candidate):
        candidate += timedelta(days=1)
    return candidate


def previous_trading_session(d: date) -> date:
    """First trading session strictly before ``d``."""
    candidate = d - timedelta(days=1)
    while not is_trading_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def session_close_cutoff(d: date) -> datetime:
    """UTC timestamp for the observation cutoff (session close) of session ``d``.

    ``d`` need not itself be a trading session; the cutoff is still returned
    for the given calendar date (callers that need "the cutoff of the actual
    session containing/covering this date" should resolve to a trading
    session first).
    """
    return datetime(d.year, d.month, d.day, _SESSION_CLOSE_CUTOFF_UTC_HOUR, 0, 0, tzinfo=timezone.utc)


def conservative_known_at_for_date_only_source(effective_date: date) -> datetime:
    """Conservative ``known_at`` for a source that supplies only a date.

    Per docs/plans/01b-research-validity-design.md Section 1.1: "A source that
    supplies only a date may be used only with a recorded, conservative
    availability rule (no earlier than the next trading session)." Returns
    the session-close cutoff of the next trading session after
    ``effective_date``, which structurally guarantees a date-only record can
    never qualify as "known" as of its own effective-start session.
    """
    return session_close_cutoff(next_trading_session(effective_date))

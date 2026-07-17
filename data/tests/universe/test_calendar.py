"""Tests for data/universe/calendar.py."""

from __future__ import annotations

from datetime import date, datetime, timezone

from data.universe.calendar import (
    conservative_known_at_for_date_only_source,
    is_trading_session,
    next_trading_session,
    previous_trading_session,
    session_close_cutoff,
)


class TestIsTradingSession:
    def test_ordinary_weekday_is_session(self) -> None:
        assert is_trading_session(date(2024, 3, 5)) is True  # Tuesday

    def test_saturday_is_not_session(self) -> None:
        assert is_trading_session(date(2024, 3, 9)) is False

    def test_sunday_is_not_session(self) -> None:
        assert is_trading_session(date(2024, 3, 10)) is False

    def test_new_years_day_is_not_session(self) -> None:
        assert is_trading_session(date(2024, 1, 1)) is False

    def test_independence_day_is_not_session(self) -> None:
        assert is_trading_session(date(2024, 7, 4)) is False

    def test_thanksgiving_is_not_session(self) -> None:
        assert is_trading_session(date(2024, 11, 28)) is False

    def test_christmas_is_not_session(self) -> None:
        assert is_trading_session(date(2024, 12, 25)) is False

    def test_good_friday_is_not_session(self) -> None:
        # Good Friday 2024 = March 29
        assert is_trading_session(date(2024, 3, 29)) is False

    def test_juneteenth_2021_onward_is_not_session(self) -> None:
        assert is_trading_session(date(2022, 6, 20)) is False  # observed Monday

    def test_juneteenth_before_2021_is_ordinary_day(self) -> None:
        # 2020-06-19 is a Friday and not yet a federal/market holiday.
        assert is_trading_session(date(2020, 6, 19)) is True


class TestNextTradingSession:
    def test_skips_weekend(self) -> None:
        # Friday -> Monday
        assert next_trading_session(date(2024, 3, 8)) == date(2024, 3, 11)

    def test_skips_holiday(self) -> None:
        # Dec 24 2024 (Tue) -> Dec 25 is Christmas -> Dec 26
        assert next_trading_session(date(2024, 12, 24)) == date(2024, 12, 26)

    def test_from_non_session_date(self) -> None:
        # Starting from a Saturday still returns the next real session.
        assert next_trading_session(date(2024, 3, 9)) == date(2024, 3, 11)


class TestPreviousTradingSession:
    def test_skips_weekend_backwards(self) -> None:
        assert previous_trading_session(date(2024, 3, 11)) == date(2024, 3, 8)


class TestSessionCloseCutoff:
    def test_returns_utc_timestamp_same_calendar_date(self) -> None:
        cutoff = session_close_cutoff(date(2024, 3, 5))
        assert cutoff.tzinfo == timezone.utc
        assert cutoff.date() == date(2024, 3, 5)


class TestConservativeKnownAt:
    def test_is_after_next_session_not_same_session(self) -> None:
        # A Wednesday change: known_at must fall on/after Thursday.
        effective = date(2024, 3, 6)  # Wednesday
        known_at = conservative_known_at_for_date_only_source(effective)
        assert known_at.date() > effective
        assert known_at.date() == date(2024, 3, 7)

    def test_is_strictly_after_effective_session_close(self) -> None:
        effective = date(2024, 3, 6)
        known_at = conservative_known_at_for_date_only_source(effective)
        assert known_at > session_close_cutoff(effective)

    def test_across_weekend(self) -> None:
        effective = date(2024, 3, 8)  # Friday
        known_at = conservative_known_at_for_date_only_source(effective)
        assert known_at.date() == date(2024, 3, 11)  # next Monday

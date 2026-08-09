"""Tests for ``strategy_registry.evaluation_window.EvaluationWindow`` (04-4W)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from strategy_registry.evaluation_window import EvaluationWindow


def test_valid_window_constructs() -> None:
    w = EvaluationWindow(start=date(2022, 1, 1), end=date(2022, 12, 31))
    assert w.start == date(2022, 1, 1)
    assert w.end == date(2022, 12, 31)


def test_equal_start_and_end_is_valid() -> None:
    """A single-day window (start == end) is a degenerate but valid case."""
    w = EvaluationWindow(start=date(2022, 1, 1), end=date(2022, 1, 1))
    assert w.start == w.end


def test_start_after_end_raises() -> None:
    with pytest.raises(ValueError, match="must be <="):
        EvaluationWindow(start=date(2022, 12, 31), end=date(2022, 1, 1))


def test_is_frozen() -> None:
    w = EvaluationWindow(start=date(2022, 1, 1), end=date(2022, 12, 31))
    with pytest.raises(AttributeError):
        w.start = date(2023, 1, 1)  # type: ignore[misc]


# ── Round-3 (PR #50 Codex P2): reject non-date values that would otherwise
#    slip past the start<=end check ─────────────────────────────────────


def test_datetime_start_raises() -> None:
    """datetime subclasses date, so isinstance(dt, date) is True and the
    start<=end comparison alone would silently accept it -- explicitly
    reject datetime, since every consumer here means a calendar date, not
    a timestamp."""
    with pytest.raises(TypeError, match="datetime.date"):
        EvaluationWindow(start=datetime(2022, 1, 1), end=date(2022, 12, 31))  # type: ignore[arg-type]


def test_datetime_end_raises() -> None:
    with pytest.raises(TypeError, match="datetime.date"):
        EvaluationWindow(start=date(2022, 1, 1), end=datetime(2022, 12, 31))  # type: ignore[arg-type]


def test_iso_string_start_raises() -> None:
    """Two ISO strings compare lexicographically the same way two dates
    would, so the start<=end check alone would not catch a string pair
    either."""
    with pytest.raises(TypeError, match="datetime.date"):
        EvaluationWindow(start="2022-01-01", end=date(2022, 12, 31))  # type: ignore[arg-type]


def test_iso_string_end_raises() -> None:
    with pytest.raises(TypeError, match="datetime.date"):
        EvaluationWindow(start=date(2022, 1, 1), end="2022-12-31")  # type: ignore[arg-type]


def test_none_start_raises() -> None:
    with pytest.raises(TypeError, match="datetime.date"):
        EvaluationWindow(start=None, end=date(2022, 12, 31))  # type: ignore[arg-type]

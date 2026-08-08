"""Tests for ``strategy_registry.evaluation_window.EvaluationWindow`` (04-4W)."""

from __future__ import annotations

from datetime import date

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

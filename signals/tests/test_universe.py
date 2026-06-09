"""Tests for signals/research/universe.py."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from signals.research.universe import (
    audit_universe_survivorship,
    label_ic_with_bias,
    universe_size_by_date,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_prices(
    tickers: list[str],
    start: date,
    n_days: int,
) -> pd.DataFrame:
    """All tickers present from start for n_days."""
    rows = []
    d = start
    seen = 0
    while seen < n_days:
        if d.weekday() < 5:
            for t in tickers:
                rows.append({"ticker": t, "date": d, "close": 100.0})
            seen += 1
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def _make_prices_with_late_entrant(
    full_tickers: list[str],
    late_tickers: list[str],
    start: date,
    late_start: date,
    n_days: int,
) -> pd.DataFrame:
    all_tickers = full_tickers + late_tickers
    rows = []
    d = start
    seen = 0
    while seen < n_days:
        if d.weekday() < 5:
            for t in all_tickers:
                if t in late_tickers and d < late_start:
                    continue
                rows.append({"ticker": t, "date": d, "close": 100.0})
            seen += 1
        d += timedelta(days=1)
    return pd.DataFrame(rows)


START = date(2021, 1, 4)
TICKERS = [f"T{i:02d}" for i in range(20)]


# ─── audit_universe_survivorship ─────────────────────────────────────────────

class TestAuditUniverseSurvivorship:
    def test_returns_dict_with_required_keys(self):
        prices = _make_prices(TICKERS, START, 100)
        result = audit_universe_survivorship(prices)
        required = {
            "total_tickers", "start_date", "end_date", "calendar_days",
            "non_late_entrant_count", "late_entrant_count", "late_entrant_fraction",
            "bias_severity", "median_history_days", "min_history_days", "warning",
        }
        assert required <= set(result.keys())

    def test_clean_universe_no_late_entrants(self):
        prices = _make_prices(TICKERS, START, 200)
        result = audit_universe_survivorship(prices)
        assert result["late_entrant_count"] == 0
        assert result["total_tickers"] == len(TICKERS)
        assert result["bias_severity"] == "low"

    def test_late_entrant_counted(self):
        late_start = START + timedelta(days=90)
        prices = _make_prices_with_late_entrant(
            full_tickers=TICKERS[:18],
            late_tickers=TICKERS[18:],
            start=START,
            late_start=late_start,
            n_days=200,
        )
        result = audit_universe_survivorship(prices)
        assert result["late_entrant_count"] == 2
        assert result["non_late_entrant_count"] == 18

    def test_high_late_entrant_fraction_classified_high(self):
        # Build a universe where 50% enter late
        late_start = START + timedelta(days=90)
        half = len(TICKERS) // 2
        prices = _make_prices_with_late_entrant(
            full_tickers=TICKERS[:half],
            late_tickers=TICKERS[half:],
            start=START,
            late_start=late_start,
            n_days=200,
        )
        result = audit_universe_survivorship(prices)
        assert result["bias_severity"] == "high"
        assert result["late_entrant_fraction"] > 0.25

    def test_moderate_severity_threshold(self):
        # ~15% late entrants → "moderate"
        n_late = 3  # 3/20 = 15%
        late_start = START + timedelta(days=90)
        prices = _make_prices_with_late_entrant(
            full_tickers=TICKERS[n_late:],
            late_tickers=TICKERS[:n_late],
            start=START,
            late_start=late_start,
            n_days=200,
        )
        result = audit_universe_survivorship(prices)
        assert result["bias_severity"] == "moderate"

    def test_warning_contains_severity(self):
        prices = _make_prices(TICKERS, START, 100)
        result = audit_universe_survivorship(prices)
        assert result["bias_severity"].upper() in result["warning"]

    def test_start_and_end_date_correct(self):
        prices = _make_prices(TICKERS[:3], START, 10)
        result = audit_universe_survivorship(prices)
        assert result["start_date"] == START

    def test_missing_column_raises(self):
        df = pd.DataFrame({"ticker": ["A"], "close": [100.0]})
        with pytest.raises(ValueError, match="missing columns"):
            audit_universe_survivorship(df)

    def test_empty_prices_raises(self):
        with pytest.raises(ValueError, match="empty"):
            audit_universe_survivorship(pd.DataFrame(columns=["ticker", "date", "close"]))


# ─── universe_size_by_date ────────────────────────────────────────────────────

class TestUniverseSizeByDate:
    def test_output_columns(self):
        prices = _make_prices(TICKERS[:5], START, 10)
        result = universe_size_by_date(prices)
        assert set(result.columns) == {"date", "ticker_count"}

    def test_correct_count(self):
        prices = _make_prices(TICKERS[:5], START, 5)
        result = universe_size_by_date(prices)
        assert (result["ticker_count"] == 5).all()

    def test_sorted_ascending(self):
        prices = _make_prices(TICKERS[:3], START, 10)
        result = universe_size_by_date(prices)
        assert list(result["date"]) == sorted(result["date"])

    def test_missing_column_raises(self):
        df = pd.DataFrame({"ticker": ["A"], "close": [1.0]})
        with pytest.raises(ValueError):
            universe_size_by_date(df)


# ─── label_ic_with_bias ───────────────────────────────────────────────────────

class TestLabelICWithBias:
    def test_adds_two_columns(self):
        prices = _make_prices(TICKERS, START, 50)
        audit = audit_universe_survivorship(prices)
        ic_df = pd.DataFrame({"horizon_days": [21, 63], "ic_mean": [0.05, 0.04]})
        result = label_ic_with_bias(ic_df, audit)
        assert "survivorship_bias_severity" in result.columns
        assert "survivorship_bias_warning" in result.columns

    def test_severity_matches_audit(self):
        prices = _make_prices(TICKERS, START, 50)
        audit = audit_universe_survivorship(prices)
        ic_df = pd.DataFrame({"horizon_days": [21]})
        result = label_ic_with_bias(ic_df, audit)
        assert (result["survivorship_bias_severity"] == audit["bias_severity"]).all()

    def test_does_not_mutate_input(self):
        prices = _make_prices(TICKERS, START, 50)
        audit = audit_universe_survivorship(prices)
        ic_df = pd.DataFrame({"horizon_days": [21]})
        original_cols = list(ic_df.columns)
        label_ic_with_bias(ic_df, audit)
        assert list(ic_df.columns) == original_cols

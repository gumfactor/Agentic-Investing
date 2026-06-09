"""Unit tests for data.normalization.completeness_checks.

All tests use synthetic DataFrames — no database or network access required.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from data.normalization.completeness_checks import (
    check_coverage,
    check_duplicates,
    check_null_prices,
    check_short_histories,
    run_completeness_checks,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_df(
    ticker_dates: dict[str, list[date]],
    close: float = 100.0,
) -> pd.DataFrame:
    """Build a long-format DataFrame from a mapping of ticker → list of dates.

    Args:
        ticker_dates: Mapping of ticker symbol to list of calendar dates.
        close: Default close price to use for every row.

    Returns:
        DataFrame with columns: ticker, date, close.
    """
    rows = []
    for ticker, dates in ticker_dates.items():
        for dt in dates:
            rows.append({"ticker": ticker, "date": dt, "close": close})
    return pd.DataFrame(rows, columns=["ticker", "date", "close"])


def _business_dates(start: date, n: int) -> list[date]:
    """Return n consecutive dates starting from start (Mon–Fri only)."""
    result: list[date] = []
    current = start
    while len(result) < n:
        if current.weekday() < 5:  # Monday=0 … Friday=4
            result.append(current)
        current += timedelta(days=1)
    return result


# ─── check_duplicates ─────────────────────────────────────────────────────────


class TestCheckDuplicates:
    def test_duplicate_pair_flagged(self) -> None:
        dt = date(2024, 1, 2)
        rows = [
            {"ticker": "AAPL", "date": dt, "close": 100.0},
            {"ticker": "AAPL", "date": dt, "close": 101.0},  # duplicate
        ]
        df = pd.DataFrame(rows)
        flags = check_duplicates(df)
        assert len(flags) == 1
        assert flags[0]["ticker"] == "AAPL"
        assert flags[0]["date"] == dt
        assert flags[0]["flag_type"] == "duplicate_row"
        assert flags[0]["severity"] == "error"

    def test_first_occurrence_not_flagged(self) -> None:
        dt = date(2024, 1, 2)
        rows = [
            {"ticker": "AAPL", "date": dt, "close": 100.0},
            {"ticker": "AAPL", "date": dt, "close": 101.0},
            {"ticker": "AAPL", "date": dt, "close": 102.0},  # second duplicate
        ]
        df = pd.DataFrame(rows)
        flags = check_duplicates(df)
        # Two duplicates after the first occurrence
        assert len(flags) == 2

    def test_different_dates_not_flagged(self) -> None:
        df = _make_df({"AAPL": [date(2024, 1, 2), date(2024, 1, 3)]})
        flags = check_duplicates(df)
        assert flags == []

    def test_different_tickers_same_date_not_flagged(self) -> None:
        dt = date(2024, 1, 2)
        df = _make_df({"AAPL": [dt], "MSFT": [dt]})
        flags = check_duplicates(df)
        assert flags == []

    def test_clean_df_returns_empty(self) -> None:
        df = _make_df({"AAPL": _business_dates(date(2024, 1, 2), 5)})
        flags = check_duplicates(df)
        assert flags == []


# ─── check_null_prices ───────────────────────────────────────────────────────


class TestCheckNullPrices:
    def test_none_close_flagged(self) -> None:
        rows = [{"ticker": "AAPL", "date": date(2024, 1, 2), "close": None}]
        df = pd.DataFrame(rows)
        flags = check_null_prices(df)
        assert len(flags) == 1
        assert flags[0]["ticker"] == "AAPL"
        assert flags[0]["flag_type"] == "null_price"
        assert flags[0]["severity"] == "error"

    def test_nan_close_flagged(self) -> None:
        import math

        rows = [{"ticker": "MSFT", "date": date(2024, 1, 3), "close": float("nan")}]
        df = pd.DataFrame(rows)
        flags = check_null_prices(df)
        assert len(flags) == 1
        assert flags[0]["ticker"] == "MSFT"
        assert flags[0]["flag_type"] == "null_price"
        assert flags[0]["severity"] == "error"

    def test_multiple_null_rows(self) -> None:
        rows = [
            {"ticker": "AAPL", "date": date(2024, 1, 2), "close": None},
            {"ticker": "AAPL", "date": date(2024, 1, 3), "close": 100.0},
            {"ticker": "MSFT", "date": date(2024, 1, 2), "close": None},
        ]
        df = pd.DataFrame(rows)
        flags = check_null_prices(df)
        assert len(flags) == 2
        flagged_tickers = {f["ticker"] for f in flags}
        assert "AAPL" in flagged_tickers
        assert "MSFT" in flagged_tickers

    def test_clean_df_returns_empty(self) -> None:
        df = _make_df({"AAPL": _business_dates(date(2024, 1, 2), 5)})
        flags = check_null_prices(df)
        assert flags == []


# ─── check_short_histories ───────────────────────────────────────────────────


class TestCheckShortHistories:
    def test_ticker_below_min_flagged(self) -> None:
        dates = _business_dates(date(2024, 1, 2), 10)
        df = _make_df({"AAPL": dates})
        flags = check_short_histories(df, min_rows=252)
        assert len(flags) == 1
        assert flags[0]["ticker"] == "AAPL"
        assert flags[0]["flag_type"] == "short_history"
        assert flags[0]["severity"] == "warning"

    def test_flag_date_is_earliest_date(self) -> None:
        dates = sorted(_business_dates(date(2024, 3, 10), 5))
        df = _make_df({"TEST": dates})
        flags = check_short_histories(df, min_rows=252)
        assert len(flags) == 1
        assert flags[0]["date"] == dates[0]

    def test_ticker_at_min_rows_not_flagged(self) -> None:
        dates = _business_dates(date(2022, 1, 3), 252)
        df = _make_df({"AAPL": dates})
        flags = check_short_histories(df, min_rows=252)
        assert flags == []

    def test_ticker_above_min_rows_not_flagged(self) -> None:
        dates = _business_dates(date(2022, 1, 3), 300)
        df = _make_df({"AAPL": dates})
        flags = check_short_histories(df, min_rows=252)
        assert flags == []

    def test_mixed_tickers_only_short_flagged(self) -> None:
        short_dates = _business_dates(date(2024, 1, 2), 50)
        long_dates = _business_dates(date(2022, 1, 3), 300)
        df = _make_df({"SHORT": short_dates, "LONG": long_dates})
        flags = check_short_histories(df, min_rows=252)
        flagged_tickers = {f["ticker"] for f in flags}
        assert "SHORT" in flagged_tickers
        assert "LONG" not in flagged_tickers

    def test_custom_min_rows(self) -> None:
        dates = _business_dates(date(2024, 1, 2), 20)
        df = _make_df({"AAPL": dates})
        # With min_rows=10, 20 rows should be fine
        flags_ok = check_short_histories(df, min_rows=10)
        assert flags_ok == []
        # With min_rows=30, 20 rows should flag
        flags_bad = check_short_histories(df, min_rows=30)
        assert len(flags_bad) == 1


# ─── check_coverage ──────────────────────────────────────────────────────────


class TestCheckCoverage:
    def test_ticker_below_threshold_flagged(self) -> None:
        # Reference ticker has 100 dates; short ticker has 50 (50% < 70%)
        ref_dates = _business_dates(date(2022, 1, 3), 100)
        short_dates = _business_dates(date(2022, 1, 3), 50)
        df = _make_df({"REF": ref_dates, "SHORT": short_dates})
        flags = check_coverage(df, coverage_threshold=0.7)
        flagged_tickers = {f["ticker"] for f in flags}
        assert "SHORT" in flagged_tickers

    def test_reference_ticker_not_flagged(self) -> None:
        ref_dates = _business_dates(date(2022, 1, 3), 100)
        short_dates = _business_dates(date(2022, 1, 3), 50)
        df = _make_df({"REF": ref_dates, "SHORT": short_dates})
        flags = check_coverage(df, coverage_threshold=0.7)
        flagged_tickers = {f["ticker"] for f in flags}
        assert "REF" not in flagged_tickers

    def test_ticker_at_threshold_not_flagged(self) -> None:
        # 70 dates vs reference of 100 → exactly 70%, should NOT be flagged
        ref_dates = _business_dates(date(2022, 1, 3), 100)
        threshold_dates = _business_dates(date(2022, 1, 3), 70)
        df = _make_df({"REF": ref_dates, "THRESHOLD": threshold_dates})
        flags = check_coverage(df, coverage_threshold=0.7)
        flagged_tickers = {f["ticker"] for f in flags}
        assert "THRESHOLD" not in flagged_tickers

    def test_flag_contains_actual_and_reference_counts(self) -> None:
        ref_dates = _business_dates(date(2022, 1, 3), 100)
        short_dates = _business_dates(date(2022, 1, 3), 40)
        df = _make_df({"REF": ref_dates, "SHORT": short_dates})
        flags = check_coverage(df, coverage_threshold=0.7)
        short_flags = [f for f in flags if f["ticker"] == "SHORT"]
        assert len(short_flags) == 1
        msg = short_flags[0]["message"]
        assert "40" in msg
        assert "100" in msg

    def test_flag_severity_is_warning(self) -> None:
        ref_dates = _business_dates(date(2022, 1, 3), 100)
        short_dates = _business_dates(date(2022, 1, 3), 30)
        df = _make_df({"REF": ref_dates, "SHORT": short_dates})
        flags = check_coverage(df, coverage_threshold=0.7)
        short_flags = [f for f in flags if f["ticker"] == "SHORT"]
        assert short_flags[0]["severity"] == "warning"

    def test_flag_type_is_low_coverage(self) -> None:
        ref_dates = _business_dates(date(2022, 1, 3), 100)
        short_dates = _business_dates(date(2022, 1, 3), 30)
        df = _make_df({"REF": ref_dates, "SHORT": short_dates})
        flags = check_coverage(df, coverage_threshold=0.7)
        short_flags = [f for f in flags if f["ticker"] == "SHORT"]
        assert short_flags[0]["flag_type"] == "low_coverage"

    def test_all_tickers_equal_coverage_no_flags(self) -> None:
        dates = _business_dates(date(2022, 1, 3), 100)
        df = _make_df({"AAPL": dates, "MSFT": dates, "GOOG": dates})
        flags = check_coverage(df, coverage_threshold=0.7)
        assert flags == []

    def test_single_ticker_no_flags(self) -> None:
        dates = _business_dates(date(2022, 1, 3), 50)
        df = _make_df({"SOLO": dates})
        flags = check_coverage(df, coverage_threshold=0.7)
        # Only one ticker — it is its own reference, so 100% coverage
        assert flags == []


# ─── run_completeness_checks ─────────────────────────────────────────────────


class TestRunCompletenessChecks:
    def test_returns_dataframe_with_correct_columns(self) -> None:
        dates = _business_dates(date(2022, 1, 3), 300)
        df = _make_df({"AAPL": dates, "MSFT": dates})
        result = run_completeness_checks(df)
        assert isinstance(result, pd.DataFrame)
        assert list(result.columns) == ["ticker", "date", "flag_type", "severity", "message"]

    def test_raises_on_missing_ticker_column(self) -> None:
        df = pd.DataFrame([{"date": date(2024, 1, 2), "close": 100.0}])
        with pytest.raises(ValueError, match="ticker"):
            run_completeness_checks(df)

    def test_raises_on_missing_date_column(self) -> None:
        df = pd.DataFrame([{"ticker": "AAPL", "close": 100.0}])
        with pytest.raises(ValueError, match="date"):
            run_completeness_checks(df)

    def test_raises_on_missing_close_column(self) -> None:
        df = pd.DataFrame([{"ticker": "AAPL", "date": date(2024, 1, 2)}])
        with pytest.raises(ValueError, match="close"):
            run_completeness_checks(df)

    def test_empty_df_returns_empty_with_correct_columns(self) -> None:
        df = pd.DataFrame(columns=["ticker", "date", "close"])
        result = run_completeness_checks(df)
        assert result.empty
        assert list(result.columns) == ["ticker", "date", "flag_type", "severity", "message"]

    def test_clean_df_returns_empty_flags(self) -> None:
        # Two tickers, both with 300 rows of valid data, equal coverage
        dates = _business_dates(date(2022, 1, 3), 300)
        df = _make_df({"AAPL": dates, "MSFT": dates})
        result = run_completeness_checks(df)
        assert result.empty
        assert list(result.columns) == ["ticker", "date", "flag_type", "severity", "message"]

    def test_collects_flags_from_all_checks(self) -> None:
        """A single batch can produce flags from multiple checks simultaneously."""
        # Duplicate row
        dt = date(2024, 1, 2)
        rows = [
            {"ticker": "AAPL", "date": dt, "close": 100.0},
            {"ticker": "AAPL", "date": dt, "close": 101.0},  # duplicate → error
            {"ticker": "MSFT", "date": dt, "close": None},   # null price → error
        ]
        df = pd.DataFrame(rows)
        result = run_completeness_checks(df)
        flag_types = set(result["flag_type"].unique())
        assert "duplicate_row" in flag_types
        assert "null_price" in flag_types

    def test_severity_column_values(self) -> None:
        # Null price → error; short history → warning
        dates_short = _business_dates(date(2024, 1, 2), 10)
        df = _make_df({"SHORT": dates_short})
        # Inject a null-close row
        null_row = pd.DataFrame([{"ticker": "SHORT", "date": date(2024, 5, 1), "close": None}])
        df = pd.concat([df, null_row], ignore_index=True)

        result = run_completeness_checks(df)
        severities = set(result["severity"].unique())
        assert "error" in severities    # from null_price
        assert "warning" in severities  # from short_history (and possibly low_coverage)

    def test_flag_format_matches_quality_checks(self) -> None:
        """Column names and dtypes are compatible with quality_checks.py output."""
        dt = date(2024, 1, 2)
        rows = [{"ticker": "AAPL", "date": dt, "close": None}]
        df = pd.DataFrame(rows)
        result = run_completeness_checks(df)
        expected_columns = {"ticker", "date", "flag_type", "severity", "message"}
        assert expected_columns == set(result.columns)
        # Values are accessible as expected
        row = result.iloc[0]
        assert isinstance(row["ticker"], str)
        assert isinstance(row["flag_type"], str)
        assert isinstance(row["severity"], str)
        assert isinstance(row["message"], str)

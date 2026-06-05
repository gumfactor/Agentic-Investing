"""Tests for point-in-time data access utilities.

These tests are the primary correctness gate for look-ahead bias prevention.
Any regression here is a critical defect — it means backtest results are
potentially based on future data.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from data.normalization.point_in_time import pit_join, pit_latest, add_ohlcv_release_date


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def simple_price_df() -> pd.DataFrame:
    """Simple OHLCV-like DataFrame with three dates for one ticker."""
    return pd.DataFrame(
        {
            "ticker": ["AAPL"] * 3,
            "date": [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)],
            "close": [150.0, 151.0, 152.0],
        }
    )


@pytest.fixture
def fundamental_df() -> pd.DataFrame:
    """Fundamental data where release_date lags period date by ~45 days."""
    return pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL", "AAPL"],
            "period_end_date": [
                date(2023, 9, 30),   # Q3 ends Sept 30
                date(2023, 12, 31),  # Q4 ends Dec 31
                date(2024, 3, 31),   # Q1 ends Mar 31
            ],
            "release_date": [
                date(2023, 11, 2),   # Announced ~33 days after quarter end
                date(2024, 2, 1),    # Announced ~32 days after quarter end
                date(2024, 5, 2),    # Announced ~32 days after quarter end
            ],
            "eps": [1.46, 2.18, 1.53],
        }
    )


# ─── pit_join tests ───────────────────────────────────────────────────────────

class TestPitJoin:
    def test_excludes_future_dates(self, simple_price_df: pd.DataFrame) -> None:
        """Core correctness: records after as_of_date must not appear."""
        result = pit_join(simple_price_df, as_of_date=date(2024, 1, 2))
        assert set(result["date"].tolist()) == {date(2024, 1, 1), date(2024, 1, 2)}
        assert date(2024, 1, 3) not in result["date"].values

    def test_includes_exact_boundary_date(self, simple_price_df: pd.DataFrame) -> None:
        """The as_of_date itself is inclusive."""
        result = pit_join(simple_price_df, as_of_date=date(2024, 1, 3))
        assert len(result) == 3

    def test_empty_dataframe_returns_empty(self) -> None:
        empty = pd.DataFrame(columns=["ticker", "date", "close"])
        result = pit_join(empty, as_of_date=date(2024, 1, 1))
        assert result.empty

    def test_all_future_returns_empty(self, simple_price_df: pd.DataFrame) -> None:
        result = pit_join(simple_price_df, as_of_date=date(2023, 12, 31))
        assert result.empty

    def test_release_date_lag_excludes_unannounced_data(
        self, fundamental_df: pd.DataFrame
    ) -> None:
        """The critical test: Q3 earnings are not visible until November, even though
        the period ended in September. Simulating on Oct 1 must not see Q3 results."""
        result = pit_join(
            fundamental_df,
            as_of_date=date(2023, 10, 1),
            date_col="period_end_date",
            release_date_col="release_date",
        )
        # Q3 period ended Sept 30 but was released Nov 2 — must be excluded
        assert result.empty

    def test_release_date_lag_includes_after_announcement(
        self, fundamental_df: pd.DataFrame
    ) -> None:
        """After the announcement date, Q3 earnings ARE visible."""
        result = pit_join(
            fundamental_df,
            as_of_date=date(2023, 11, 15),
            date_col="period_end_date",
            release_date_col="release_date",
        )
        assert len(result) == 1
        assert result.iloc[0]["period_end_date"] == date(2023, 9, 30)

    def test_multiple_quarters_visible_at_year_end(
        self, fundamental_df: pd.DataFrame
    ) -> None:
        """On Feb 2, 2024 both Q3 and Q4 earnings are visible."""
        result = pit_join(
            fundamental_df,
            as_of_date=date(2024, 2, 2),
            date_col="period_end_date",
            release_date_col="release_date",
        )
        assert len(result) == 2

    def test_raises_on_missing_date_col(self, simple_price_df: pd.DataFrame) -> None:
        with pytest.raises(KeyError, match="nonexistent_col"):
            pit_join(simple_price_df, as_of_date=date(2024, 1, 1), date_col="nonexistent_col")

    def test_raises_on_missing_release_date_col(self, simple_price_df: pd.DataFrame) -> None:
        with pytest.raises(KeyError, match="release_date"):
            pit_join(
                simple_price_df,
                as_of_date=date(2024, 1, 1),
                release_date_col="release_date",
            )

    def test_preserves_dtypes_and_values(self, simple_price_df: pd.DataFrame) -> None:
        result = pit_join(simple_price_df, as_of_date=date(2024, 1, 2))
        assert result["close"].tolist() == [150.0, 151.0]

    def test_does_not_mutate_input(self, simple_price_df: pd.DataFrame) -> None:
        original_len = len(simple_price_df)
        pit_join(simple_price_df, as_of_date=date(2024, 1, 1))
        assert len(simple_price_df) == original_len


# ─── pit_latest tests ─────────────────────────────────────────────────────────

class TestPitLatest:
    def test_returns_most_recent_visible_per_ticker(
        self, fundamental_df: pd.DataFrame
    ) -> None:
        """On Feb 10, 2024, the latest visible quarter for AAPL is Q4."""
        result = pit_latest(
            fundamental_df,
            as_of_date=date(2024, 2, 10),
            group_cols=["ticker"],
            date_col="period_end_date",
            release_date_col="release_date",
        )
        assert len(result) == 1
        assert result.iloc[0]["period_end_date"] == date(2023, 12, 31)

    def test_empty_on_no_visible_data(self, fundamental_df: pd.DataFrame) -> None:
        result = pit_latest(
            fundamental_df,
            as_of_date=date(2022, 1, 1),
            group_cols=["ticker"],
            date_col="period_end_date",
            release_date_col="release_date",
        )
        assert result.empty


# ─── add_ohlcv_release_date tests ─────────────────────────────────────────────

class TestAddOhlcvReleaseDate:
    def test_release_date_equals_date(self, simple_price_df: pd.DataFrame) -> None:
        result = add_ohlcv_release_date(simple_price_df)
        assert "release_date" in result.columns
        assert (result["release_date"] == result["date"]).all()

    def test_does_not_mutate_input(self, simple_price_df: pd.DataFrame) -> None:
        add_ohlcv_release_date(simple_price_df)
        assert "release_date" not in simple_price_df.columns
